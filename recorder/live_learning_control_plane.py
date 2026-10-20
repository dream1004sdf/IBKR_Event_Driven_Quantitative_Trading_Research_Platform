from __future__ import annotations

import argparse
import json
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .analysis_database import AnalysisDatabase
from .geopolitical_intelligence import (
    build_geopolitical_heatmap_from_connection,
    build_geopolitical_intelligence_report,
    write_geopolitical_intelligence_json,
    write_geopolitical_intelligence_markdown,
)
from .singleton_lock import AlreadyRunningError, SingleInstanceLock
from .standing_regimes import (
    build_standing_regime_outcome_report,
    standing_regime_runtime_metrics,
    write_standing_regime_outcome_json,
    write_standing_regime_outcome_markdown,
)


LIVE_LEARNING_CONTROL_PLANE_VERSION = "m232-live-learning-control-plane-v1"


@dataclass(frozen=True)
class LiveLearningControlPlaneConfig:
    database_path: Path
    live_scorer_learning_json_path: Path | None = None
    threshold_candidate_evaluator_json_path: Path | None = None
    limited_auto_readiness_json_path: Path | None = Path("exports/m250_limited_auto_trading_readiness.json")
    option_outcome_limit: int = 5000
    emergency_limit: int = 5000
    action_gate_limit: int = 5000
    max_ticks: int = 1
    interval_seconds: float = 60.0
    sleep_between_ticks: bool = False
    adaptive_enabled: bool = True
    hot_limit: int = 500
    hot_min_interval_seconds: float = 5.0
    hot_normal_interval_seconds: float = 15.0
    hot_max_interval_seconds: float = 60.0
    full_report_interval_seconds: float = 300.0
    hot_cache_json_path: Path | None = None
    reflex_hot_cache_json_path: Path | None = None
    scorer_overlay_cache_json_path: Path | None = None
    option_structure_cache_json_path: Path | None = None
    action_gate_cache_json_path: Path | None = None
    lock_path: Path | None = Path("locks/live_learning_control_plane.lock")
    output_path: Path | None = None
    json_output_path: Path | None = None
    refresh_context_learning: bool = False
    context_refresh_lookback_hours: float = 168.0
    context_refresh_signal_limit: int = 2000
    context_refresh_horizons_minutes: str = "5,15,30,390"
    context_refresh_max_assets_per_signal: int = 6
    context_refresh_max_outcome_lag_minutes: float = 1440.0
    context_refresh_evaluation_limit: int = 5000
    context_refresh_symbols: str = "SPY,QQQ,IWM,DIA,SMH,SOXX,USO,XLE,GLD,VIX"
    geopolitical_intelligence_json_path: Path | None = Path("exports/geopolitical_intelligence.json")
    geopolitical_intelligence_output_path: Path | None = Path("exports/geopolitical_intelligence.md")
    standing_regime_outcomes_json_path: Path | None = Path("exports/standing_regime_outcomes.json")
    standing_regime_outcomes_output_path: Path | None = Path("exports/standing_regime_outcomes.md")


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload.get("result") if isinstance(payload.get("result"), dict) else payload


def _atomic_write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.expanduser().resolve(), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    return bool(row)


def _safe_json(value: Any, default: Any) -> Any:
    try:
        if value in (None, ""):
            return default
        return json.loads(str(value))
    except Exception:
        return default


def _direction_from_return(raw_return: Any, threshold: float = 0.001) -> str:
    try:
        value = float(raw_return)
    except (TypeError, ValueError):
        return "unknown"
    if value >= threshold:
        return "up"
    if value <= -threshold:
        return "down"
    return "flat"


def _parse_iso(value: Any) -> datetime | None:
    try:
        text = str(value or "").strip()
        if not text:
            return None
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


def _age_seconds(value: Any) -> float | None:
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    return round((datetime.now(UTC) - parsed).total_seconds(), 3)


def compact_m232_hot_cache_for_scorer(path: Path | None, *, max_age_seconds: float = 180.0) -> dict[str, Any]:
    payload = _read_json(path)
    if not payload:
        return {
            "version": "m232-scorer-hot-cache-context-v1",
            "available": False,
            "usable": False,
            "status": "missing",
            "reason": "m232_hot_cache_missing",
        }
    generated_at = payload.get("generated_at")
    embedded_max_age = payload.get("max_consumer_age_seconds")
    try:
        max_age = float(embedded_max_age if embedded_max_age is not None else max_age_seconds)
    except (TypeError, ValueError):
        max_age = float(max_age_seconds)
    age = _age_seconds(generated_at)
    stale = age is None or age > max_age
    scorer_overlay = payload.get("scorer_overlay_cache") or {}
    reflex = payload.get("reflex_hot_cache") or {}
    option = payload.get("option_structure_cache") or {}
    action = payload.get("action_gate_cache") or {}
    standing_regimes = payload.get("standing_regime_cache") or scorer_overlay.get("standing_regime_cache") or {}
    geopolitical = payload.get("geopolitical_hot_cache") or scorer_overlay.get("geopolitical_hot_cache") or {}
    return {
        "version": "m232-scorer-hot-cache-context-v1",
        "available": True,
        "usable": not stale,
        "status": "stale" if stale else "fresh",
        "generated_at": generated_at,
        "age_seconds": age,
        "max_age_seconds": max_age,
        "adaptive_tier": (payload.get("adaptive_schedule") or {}).get("tier"),
        "adaptive_next_interval_seconds": (payload.get("adaptive_schedule") or {}).get("next_interval_seconds"),
        "paper_scorer_ready": bool(scorer_overlay.get("paper_scorer_ready")),
        "conditional_overlay_available": bool(scorer_overlay.get("conditional_overlay_available")),
        "scorer_withhold_reasons": scorer_overlay.get("withhold_reasons") or {},
        "standing_regime_count": int(standing_regimes.get("active_or_easing_count") or 0),
        "standing_regime_bias_counts": standing_regimes.get("bias_counts") or {},
        "standing_regime_family_counts": standing_regimes.get("family_counts") or {},
        "standing_regime_top": (standing_regimes.get("top_regimes") or [])[:5],
        "standing_regime_cache": {
            "available": bool(standing_regimes.get("available")),
            "generated_at": standing_regimes.get("generated_at"),
            "fresh_count": standing_regimes.get("fresh_count"),
            "aging_count": standing_regimes.get("aging_count"),
            "resolution_watch_count": standing_regimes.get("resolution_watch_count"),
        },
        "geopolitical_hot_bucket_count": int(geopolitical.get("hot_bucket_count") or 0),
        "geopolitical_heat_color_counts": geopolitical.get("heat_color_counts") or {},
        "geopolitical_top_buckets": (geopolitical.get("top_buckets") or [])[:5],
        "reflex_candidate_count": int(reflex.get("reflex_candidate_count") or 0),
        "reflex_top_assets": reflex.get("top_assets") or {},
        "reflex_memories": (reflex.get("memories") or [])[:5],
        "option_memory_count": int(option.get("memory_count") or 0),
        "option_memories": (option.get("memories") or [])[:5],
        "action_gate_memory_count": int(action.get("memory_count") or 0),
        "action_gate_paper_entry_candidate_count": int(action.get("paper_entry_candidate_count") or 0),
        "action_gate_block_or_second_pass_count": int(action.get("block_or_second_pass_count") or 0),
        "action_gate_memories": (action.get("memories") or [])[:5],
        "safety": {
            "hot_path_cache_only": True,
            "db_full_scan_required_by_hot_path": False,
            "live_scorer_mutated": False,
            "threshold_table_mutated": False,
            "broker_orders_submitted": False,
        },
    }


def _m250_gate_severity_overlay(m250: dict[str, Any] | None) -> dict[str, Any]:
    data = m250 or {}
    contexts = data.get("contexts") if isinstance(data.get("contexts"), dict) else {}
    policy = contexts.get("gate_severity_policy") if isinstance(contexts.get("gate_severity_policy"), dict) else {}
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    if not policy and not summary:
        return {"available": False, "reason": "m250_gate_severity_policy_missing"}
    return {
        "available": True,
        "source_version": data.get("limited_auto_trading_readiness_version") or data.get("version"),
        "source_generated_at": data.get("generated_at"),
        "posture": summary.get("gate_execution_posture") or policy.get("recommended_execution_posture"),
        "size_multiplier": summary.get("gate_size_multiplier") or policy.get("size_multiplier"),
        "confidence_multiplier": summary.get("gate_confidence_multiplier") or policy.get("confidence_multiplier"),
        "category_counts": policy.get("category_counts") or {},
        "signals": (policy.get("signals") or [])[:20],
        "scorer_action": "confidence_multiplier",
        "action_gate_action": "size_multiplier_or_trade_now_block",
    }


def _promotion_gate_application(m226: dict[str, Any], m250: dict[str, Any] | None = None) -> dict[str, Any]:
    gate = m226.get("promotion_gate") or {}
    waitlist = gate.get("waitlist_candidates") or []
    watchlist = gate.get("watchlist_candidates") or []
    hooks = gate.get("scorer_evidence_packet_hooks") or []
    policies = gate.get("scorer_threshold_application_policies") or []
    ready = int(m226.get("promotion_ready_count") or 0)
    conditional_overlay_available = bool(hooks or policies)
    paper_scorer_ready = bool(waitlist)
    gate_overlay = _m250_gate_severity_overlay(m250)
    if gate_overlay.get("available"):
        policies = [
            *policies,
            {
                "source": "m250_gate_severity_policy",
                "scorer_action": "confidence_multiplier",
                "confidence_multiplier": gate_overlay.get("confidence_multiplier"),
                "execution_posture": gate_overlay.get("posture"),
                "size_multiplier": gate_overlay.get("size_multiplier"),
            },
        ]
    stages = [
        {
            "stage": "paper_scorer",
            "status": (
                "ready"
                if paper_scorer_ready
                else "conditional_overlay_waiting_for_release_conditions"
                if conditional_overlay_available
                else "observe_only_waitlist_empty"
            ),
            "allowed": paper_scorer_ready,
            "description": "Apply promoted threshold/scorer memory as an evidence-packet overlay only.",
        },
        {
            "stage": "paper_execution",
            "status": "paper_only_ready" if paper_scorer_ready else "blocked_until_paper_scorer_ready",
            "allowed": paper_scorer_ready,
            "description": "Convert scorer output to paper intents only; no broker order route.",
        },
        {
            "stage": "what_if_margin_option_safety",
            "status": "required_before_any_live_release",
            "allowed": False,
            "description": "Broker what-if, margin, option quote/liquidity, assignment, borrow/locate, and risk cache must pass.",
        },
        {
            "stage": "live_read_only_validation",
            "status": "required_after_paper_stability",
            "allowed": False,
            "description": "Read-only live account/market validation before any limited automation.",
        },
        {
            "stage": "limited_auto_trading",
            "status": "blocked_by_design",
            "allowed": False,
            "description": "Explicit future release gate only; this artifact never enables transmit or live routing.",
        },
    ]
    return {
        "version": "m232-live-scorer-paper-application-v1",
        "application_mode": "paper_evidence_packet_overlay",
        "promotion_ready_count": ready,
        "waitlist_count": int(gate.get("waitlist_count") or 0),
        "watchlist_count": int(gate.get("watchlist_count") or 0),
        "blocked_count": int(gate.get("blocked_count") or 0),
        "paper_scorer_ready": paper_scorer_ready,
        "conditional_overlay_available": conditional_overlay_available,
        "scorer_evidence_packet_hooks": hooks[:20],
        "scorer_threshold_application_policies": policies[:20],
        "gate_severity_overlay": gate_overlay,
        "watchlist_detail_reason_counts": gate.get("watchlist_detail_reason_counts") or {},
        "blocked_detail_reason_counts": gate.get("blocked_detail_reason_counts") or {},
        "stage_sequence": stages,
        "safety": {
            "live_scorer_mutated": False,
            "threshold_table_mutated": False,
            "paper_execution_only": True,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def _emergency_reflex_memory(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    if not _table_exists(conn, "external_market_signals"):
        return {"version": "m232-emergency-reflex-memory-v1", "available": False, "reason": "external_market_signals_missing"}
    rows = conn.execute(
        """
        SELECT id,title,published_at,region,theme,sentiment_score,sentiment_label,confidence,
               assets_json,market_reaction_status,market_reaction_json,prediction_gate_status,raw_json
          FROM external_market_signals
         WHERE source='emergency_breaking_news'
         ORDER BY COALESCE(NULLIF(published_at,''), created_at) DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    urgency_counts: Counter[str] = Counter()
    asset_counts: Counter[str] = Counter()
    for row in rows:
        raw = _safe_json(row["raw_json"], {})
        urgency_obj = raw.get("urgency") if isinstance(raw, dict) else {}
        urgency = str((urgency_obj or {}).get("urgency") or "unknown")
        assets = _safe_json(row["assets_json"], [])
        asset_keys: list[str] = []
        for item in assets:
            if isinstance(item, dict):
                asset = item.get("symbol") or item.get("asset") or item.get("ticker") or item.get("key")
            else:
                asset = item
            text = str(asset or "").strip().upper()
            if text:
                asset_keys.append(text)
        if not asset_keys:
            asset_keys = ["UNKNOWN"]
        urgency_counts.update([urgency])
        asset_counts.update(asset_keys)
        reaction = _safe_json(row["market_reaction_json"], {})
        reaction_direction = str(reaction.get("direction") or reaction.get("actual_direction") or "unknown")
        if reaction_direction == "unknown":
            reaction_direction = _direction_from_return(reaction.get("raw_return") or reaction.get("return"))
        for asset in asset_keys[:6]:
            region = row["region"] or "unknown"
            theme = row["theme"] or "unknown"
            key = f"region={region}|theme={theme}|urgency={urgency}|asset={asset}"
            item = groups.setdefault(
                key,
                {
                    "pattern_key": key,
                    "urgency": urgency,
                    "asset": asset,
                    "theme": theme,
                    "region": region,
                    "seen": 0,
                    "reaction_direction_counts": Counter(),
                    "sentiment_scores": [],
                    "confidence_scores": [],
                    "sample_titles": [],
                },
            )
            item["seen"] += 1
            item["reaction_direction_counts"].update([reaction_direction])
            if row["sentiment_score"] is not None:
                item["sentiment_scores"].append(float(row["sentiment_score"]))
            if row["confidence"] is not None:
                item["confidence_scores"].append(float(row["confidence"]))
            if len(item["sample_titles"]) < 3:
                item["sample_titles"].append(row["title"])
    memories: list[dict[str, Any]] = []
    for item in groups.values():
        counts = item.pop("reaction_direction_counts")
        top_direction, top_count = counts.most_common(1)[0] if counts else ("unknown", 0)
        sentiment_scores = item.pop("sentiment_scores")
        confidence_scores = item.pop("confidence_scores")
        avg_sentiment = sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else None
        avg_confidence = sum(confidence_scores) / len(confidence_scores) if confidence_scores else None
        stability = top_count / item["seen"] if item["seen"] else 0.0
        if item["seen"] >= 5 and stability >= 0.6 and top_direction != "unknown":
            action = "reflex_candidate"
        elif item["seen"] >= 2:
            action = "collect_more_samples"
        else:
            action = "observe_only"
        memories.append(
            {
                **item,
                "dominant_reaction_direction": top_direction,
                "dominant_reaction_share": round(stability, 4),
                "reaction_direction_counts": dict(counts),
                "avg_sentiment_score": round(avg_sentiment, 4) if avg_sentiment is not None else None,
                "avg_confidence": round(avg_confidence, 4) if avg_confidence is not None else None,
                "recommended_reflex_action": action,
                "llm_required_before_action": action != "reflex_candidate",
                "reflex_threshold": {
                    "min_seen": 5,
                    "min_dominant_reaction_share": 0.6,
                    "max_llm_latency_dependency": "none_after_promotion",
                },
            }
        )
    memories.sort(key=lambda item: (item["recommended_reflex_action"] != "reflex_candidate", -item["seen"], -float(item["dominant_reaction_share"] or 0)))
    return {
        "version": "m232-emergency-reflex-memory-v1",
        "available": True,
        "source_rows": len(rows),
        "memory_count": len(memories),
        "reflex_candidate_count": sum(1 for item in memories if item["recommended_reflex_action"] == "reflex_candidate"),
        "urgency_counts": dict(urgency_counts),
        "top_assets": dict(asset_counts.most_common(20)),
        "memories": memories[:50],
        "safety": {
            "llm_bypass_is_shadow_only": True,
            "live_order_routing_enabled": False,
            "threshold_table_mutated": False,
        },
    }


def _option_structure_learning(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    if not _table_exists(conn, "option_structure_outcome_logs"):
        return {"version": "m232-option-structure-learning-v1", "available": False, "reason": "option_structure_outcome_logs_missing"}
    rows = conn.execute(
        """
        SELECT symbol,horizon,raw_return,direction_label,price_status,gamma_regime,
               distance_to_pin_pct,pin_risk,proximity_alerts_json,expiry_bucket_summary_json,
               confidence,confidence_score
          FROM option_structure_outcome_logs
         ORDER BY id DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    group_stats: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row["price_status"] != "complete":
            continue
        proximity = _safe_json(row["proximity_alerts_json"], [])
        buckets = _safe_json(row["expiry_bucket_summary_json"], [])
        tags = [
            f"gamma={row['gamma_regime'] or 'unknown'}",
            f"horizon={row['horizon'] or 'unknown'}",
            "pin_risk" if int(row["pin_risk"] or 0) else "no_pin_risk",
            "near_wall" if proximity else "no_wall_alert",
        ]
        for bucket in buckets:
            if isinstance(bucket, dict):
                bucket_name = str(bucket.get("bucket") or "")
                if bucket_name:
                    tags.append(f"dte_bucket={bucket_name}")
                    break
        key = "|".join(tags)
        stat = group_stats.setdefault(
            key,
            {
                "pattern_key": key,
                "seen": 0,
                "direction_counts": Counter(),
                "returns": [],
                "symbols": Counter(),
                "confidence_counts": Counter(),
                "confidence_scores": [],
            },
        )
        stat["seen"] += 1
        stat["direction_counts"].update([row["direction_label"] or _direction_from_return(row["raw_return"])])
        if row["raw_return"] is not None:
            stat["returns"].append(float(row["raw_return"]))
        stat["symbols"].update([row["symbol"]])
        stat["confidence_counts"].update([row["confidence"] or "unknown"])
        if row["confidence_score"] is not None:
            stat["confidence_scores"].append(float(row["confidence_score"]))
    memories: list[dict[str, Any]] = []
    for stat in group_stats.values():
        returns = stat.pop("returns")
        directions = stat.pop("direction_counts")
        symbols = stat.pop("symbols")
        confidence_counts = stat.pop("confidence_counts")
        confidence_scores = stat.pop("confidence_scores")
        avg_return = sum(returns) / len(returns) if returns else None
        abs_avg = sum(abs(value) for value in returns) / len(returns) if returns else None
        avg_confidence_score = sum(confidence_scores) / len(confidence_scores) if confidence_scores else None
        top_direction, top_count = directions.most_common(1)[0] if directions else ("unknown", 0)
        share = top_count / stat["seen"] if stat["seen"] else 0.0
        non_low_confidence_count = int(confidence_counts.get("medium") or 0) + int(confidence_counts.get("high") or 0)
        non_low_confidence_share = non_low_confidence_count / stat["seen"] if stat["seen"] else 0.0
        if "gamma=positive_gamma_mean_reversion_bias" in stat["pattern_key"]:
            expected = "mean_reversion_or_pin"
        elif "gamma=negative_gamma_trend_amplification_bias" in stat["pattern_key"]:
            expected = "trend_amplification"
        else:
            expected = "context_only"
        if stat["seen"] >= 12 and non_low_confidence_share >= 0.5 and share >= 0.6 and expected != "context_only":
            recommended_use = "threshold_candidate_context"
            scorer_use_strength = "strong"
        elif stat["seen"] >= 6 and non_low_confidence_share >= 0.25:
            recommended_use = "scorer_evidence_packet_context"
            scorer_use_strength = "medium"
        elif stat["seen"] >= 3:
            recommended_use = "low_confidence_context_only"
            scorer_use_strength = "weak"
        else:
            recommended_use = "collect_more_outcomes"
            scorer_use_strength = "observe"
        memories.append(
            {
                **stat,
                "dominant_direction": top_direction,
                "dominant_direction_share": round(share, 4),
                "direction_counts": dict(directions),
                "avg_return": round(avg_return, 6) if avg_return is not None else None,
                "avg_abs_return": round(abs_avg, 6) if abs_avg is not None else None,
                "top_symbols": dict(symbols.most_common(10)),
                "confidence_counts": dict(confidence_counts),
                "avg_confidence_score": round(avg_confidence_score, 3) if avg_confidence_score is not None else None,
                "non_low_confidence_count": non_low_confidence_count,
                "non_low_confidence_share": round(non_low_confidence_share, 4),
                "expected_behavior": expected,
                "recommended_use": recommended_use,
                "scorer_use_strength": scorer_use_strength,
                "scorer_injection_policy": {
                    "strong": "may influence scorer option-structure adjustment after promotion gates",
                    "medium": "inject as evidence packet context with small weight",
                    "weak": "show as context only; do not adjust thresholds",
                    "observe": "collect more outcomes before scorer use",
                }[scorer_use_strength],
            }
        )
    memories.sort(key=lambda item: (
        {"strong": 0, "medium": 1, "weak": 2, "observe": 3}.get(str(item.get("scorer_use_strength")), 9),
        -item["seen"],
        -float(item["avg_abs_return"] or 0),
    ))
    return {
        "version": "m232-option-structure-learning-v1",
        "available": True,
        "source_rows": len(rows),
        "complete_rows": sum(1 for row in rows if row["price_status"] == "complete"),
        "memory_count": len(memories),
        "scorer_use_strength_counts": dict(Counter(str(item.get("scorer_use_strength") or "unknown") for item in memories)),
        "threshold_candidate_context_count": sum(1 for item in memories if item.get("recommended_use") == "threshold_candidate_context"),
        "context_only_count": sum(1 for item in memories if item.get("recommended_use") in {"scorer_evidence_packet_context", "low_confidence_context_only"}),
        "memories": memories[:50],
        "tracked_concepts": [
            "GEX regime actual reaction",
            "max pain / pinning",
            "call-put wall proximity",
            "0DTE / near-expiry bucket pressure",
            "positive gamma mean reversion",
            "negative gamma trend amplification",
        ],
    }


def _action_gate_learning(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    if not (_table_exists(conn, "m223_live_learning_predictions") and _table_exists(conn, "m223_live_learning_outcomes")):
        return {"version": "m232-action-gate-learning-v1", "available": False, "reason": "m223_live_learning_tables_missing"}
    rows = conn.execute(
        """
        SELECT p.symbol,p.predicted_direction,p.expected_return,p.confidence,p.packet_quality_level,
               p.timestamp_alignment_status,p.baseline_is_late,p.option_structure_context_json,
               o.horizon_minutes,o.raw_return,o.actual_direction,o.direction_hit,o.absolute_error,o.status
          FROM m223_live_learning_outcomes o
          JOIN m223_live_learning_predictions p ON p.id=o.prediction_id
         WHERE o.status='evaluated'
         ORDER BY o.evaluated_at DESC
         LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        option_context = _safe_json(row["option_structure_context_json"], {})
        option_fresh = str(option_context.get("freshness_status") or "unknown")
        key = (
            f"pred={row['predicted_direction'] or 'unknown'}|"
            f"quality={row['packet_quality_level'] or 'unknown'}|"
            f"time={row['timestamp_alignment_status'] or 'unknown'}|"
            f"option={option_fresh}|"
            f"h={row['horizon_minutes']}"
        )
        stat = groups.setdefault(
            key,
            {
                "pattern_key": key,
                "seen": 0,
                "hits": 0,
                "errors": [],
                "returns": [],
                "symbols": Counter(),
                "actual_direction_counts": Counter(),
            },
        )
        stat["seen"] += 1
        stat["hits"] += int(row["direction_hit"] or 0)
        if row["absolute_error"] is not None:
            stat["errors"].append(float(row["absolute_error"]))
        if row["raw_return"] is not None:
            stat["returns"].append(float(row["raw_return"]))
        stat["symbols"].update([row["symbol"]])
        stat["actual_direction_counts"].update([row["actual_direction"] or "unknown"])
    memories: list[dict[str, Any]] = []
    for stat in groups.values():
        errors = stat.pop("errors")
        returns = stat.pop("returns")
        symbols = stat.pop("symbols")
        actual_counts = stat.pop("actual_direction_counts")
        hit_rate = stat["hits"] / stat["seen"] if stat["seen"] else None
        avg_error = sum(errors) / len(errors) if errors else None
        avg_abs_return = sum(abs(value) for value in returns) / len(returns) if returns else None
        if stat["seen"] >= 8 and hit_rate is not None and hit_rate >= 0.58:
            action = "paper_entry_candidate"
        elif stat["seen"] >= 8 and hit_rate is not None and hit_rate < 0.45:
            action = "block_or_second_pass"
        else:
            action = "collect_more_samples"
        memories.append(
            {
                **stat,
                "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
                "avg_absolute_error": round(avg_error, 6) if avg_error is not None else None,
                "avg_abs_return": round(avg_abs_return, 6) if avg_abs_return is not None else None,
                "top_symbols": dict(symbols.most_common(10)),
                "actual_direction_counts": dict(actual_counts),
                "recommended_action_gate_policy": action,
                "paper_execution_allowed": action == "paper_entry_candidate",
                "requires_whatif_margin_option_safety": action == "paper_entry_candidate",
            }
        )
    memories.sort(key=lambda item: (item["recommended_action_gate_policy"] != "paper_entry_candidate", -item["seen"], -(item["hit_rate"] or 0)))
    return {
        "version": "m232-action-gate-learning-v1",
        "available": True,
        "source_rows": len(rows),
        "memory_count": len(memories),
        "paper_entry_candidate_count": sum(1 for item in memories if item["recommended_action_gate_policy"] == "paper_entry_candidate"),
        "block_or_second_pass_count": sum(1 for item in memories if item["recommended_action_gate_policy"] == "block_or_second_pass"),
        "memories": memories[:50],
        "questions_answered": [
            "prediction_correct_but_timing_bad",
            "option_strategy_vs_stock_needs_separate_paper_result",
            "hedge_requirement_by_context",
            "risk_gate_block_correctness",
            "would_have_traded_pnl_proxy",
        ],
        "safety": {
            "paper_execution_allowed_only_after_gate": True,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def build_live_learning_control_plane(config: LiveLearningControlPlaneConfig) -> dict[str, Any]:
    m223 = _read_json(config.live_scorer_learning_json_path)
    m226 = _read_json(config.threshold_candidate_evaluator_json_path)
    m250 = _read_json(config.limited_auto_readiness_json_path)
    with _connect(config.database_path) as conn:
        emergency = _emergency_reflex_memory(conn, limit=config.emergency_limit)
        option_learning = _option_structure_learning(conn, limit=config.option_outcome_limit)
        action_learning = _action_gate_learning(conn, limit=config.action_gate_limit)
        standing_regimes = _standing_regime_hot_cache(conn, limit=min(max(config.hot_limit, 50), 250))
        geopolitical = _geopolitical_hot_cache(conn, limit=min(max(config.hot_limit, 50), 250))
    application = _promotion_gate_application(m226, m250)
    return {
        "live_learning_control_plane_version": LIVE_LEARNING_CONTROL_PLANE_VERSION,
        "generated_at": _iso_now(),
        "database_path": str(config.database_path.expanduser().resolve()),
        "live_scorer_application": application,
        "emergency_news_reflex_memory": emergency,
        "options_structure_learning": option_learning,
        "standing_regime_overlay_learning": standing_regimes,
        "geopolitical_overlay_learning": geopolitical,
        "live_paper_action_gate_learning": action_learning,
        "live_scorer_learning_snapshot": {
            "status": m223.get("latest_status"),
            "prediction_count": (m223.get("latest_counts") or {}).get("prediction_count"),
            "evaluated_outcome_count": (m223.get("latest_counts") or {}).get("evaluated_outcome_count"),
            "adjustment_note_count": (m223.get("latest_counts") or {}).get("adjustment_note_count"),
            "latest_prediction_at": (m223.get("latest_counts") or {}).get("latest_prediction_at"),
            "latest_evaluated_at": (m223.get("latest_counts") or {}).get("latest_evaluated_at"),
        },
        "recommended_next_step": (
            "feed paper_scorer overlay into scorer evidence packets"
            if application.get("paper_scorer_ready")
            else "continue M223/M225/M226 collection until promotion waitlist has applicable hooks"
        ),
        "safety": {
            "read_only_report": True,
            "shadow_only": True,
            "live_scorer_mutated": False,
            "threshold_table_mutated": False,
            "paper_orders_created": False,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def live_learning_control_plane_markdown(result: dict[str, Any]) -> str:
    application = result.get("live_scorer_application") or {}
    emergency = result.get("emergency_news_reflex_memory") or {}
    options = result.get("options_structure_learning") or {}
    standing = result.get("standing_regime_overlay_learning") or {}
    geopolitical = result.get("geopolitical_overlay_learning") or {}
    action = result.get("live_paper_action_gate_learning") or {}
    context_refresh = result.get("context_learning_refresh") or {}
    lines = [
        "# M232 Live Learning Control Plane",
        "",
        f"- Version: `{result.get('live_learning_control_plane_version')}`",
        f"- Generated: `{result.get('generated_at')}`",
        f"- Recommended next step: `{result.get('recommended_next_step')}`",
        f"- Context learning refresh: enabled `{context_refresh.get('enabled')}` ok `{context_refresh.get('ok')}` errors `{context_refresh.get('errors') or []}`",
        "",
        "## 1. Live Scorer paper application path",
        "",
        f"- Mode: `{application.get('application_mode')}`",
        f"- Promotion ready / waitlist / watchlist / blocked: `{application.get('promotion_ready_count')}` / `{application.get('waitlist_count')}` / `{application.get('watchlist_count')}` / `{application.get('blocked_count')}`",
        f"- Paper scorer ready: `{application.get('paper_scorer_ready')}`",
        f"- Gate severity overlay: `{(application.get('gate_severity_overlay') or {}).get('posture') or 'n/a'}` confidence x`{(application.get('gate_severity_overlay') or {}).get('confidence_multiplier')}` size x`{(application.get('gate_severity_overlay') or {}).get('size_multiplier')}`",
        "",
        "| Stage | Status | Allowed | Description |",
        "| --- | --- | --- | --- |",
    ]
    for stage in application.get("stage_sequence") or []:
        lines.append(f"| `{stage.get('stage')}` | `{stage.get('status')}` | `{stage.get('allowed')}` | {stage.get('description')} |")
    lines.extend(
        [
            "",
            "## 2. Emergency News Reflex Memory",
            "",
            f"- Available: `{emergency.get('available')}` rows `{emergency.get('source_rows')}` memories `{emergency.get('memory_count')}` reflex candidates `{emergency.get('reflex_candidate_count')}`",
            f"- Urgency counts: `{emergency.get('urgency_counts') or {}}`",
            f"- Top assets: `{emergency.get('top_assets') or {}}`",
            "",
            "| Action | Seen | Direction | Share | Pattern | Samples |",
            "| --- | ---: | --- | ---: | --- | --- |",
        ]
    )
    for item in (emergency.get("memories") or [])[:10]:
        lines.append(
            f"| `{item.get('recommended_reflex_action')}` | {item.get('seen')} | `{item.get('dominant_reaction_direction')}` | "
            f"{item.get('dominant_reaction_share')} | `{item.get('pattern_key')}` | {str(item.get('sample_titles') or [])[:180].replace('|', '/')} |"
        )
    if not emergency.get("memories"):
        lines.append("| - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 3. Geopolitical Overlay Hot Cache",
            "",
            f"- Available: `{geopolitical.get('available')}` buckets `{geopolitical.get('bucket_count')}` hot buckets `{geopolitical.get('hot_bucket_count')}`",
            f"- Heat colors: `{geopolitical.get('heat_color_counts') or {}}`",
            "",
            "| Heat | Score | Region | Theme | Signals | Urgency | Assets | Action | Latest |",
            "| --- | ---: | --- | --- | ---: | --- | --- | --- | --- |",
        ]
    )
    for item in (geopolitical.get("top_buckets") or [])[:12]:
        lines.append(
            f"| `{item.get('heat_color')}` | {item.get('heat_score')} | `{item.get('region')}` | `{item.get('theme')}` | "
            f"{item.get('signal_count')} | `{item.get('urgency_counts')}` | `{item.get('top_assets')}` | "
            f"`{item.get('desk_action')}` | {str(item.get('latest_titles') or [])[:180].replace('|', '/')} |"
        )
    if not geopolitical.get("top_buckets"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 4. Options Structure Learning",
            "",
            f"- Available: `{options.get('available')}` complete rows `{options.get('complete_rows')}` memories `{options.get('memory_count')}`",
            f"- Scorer strength counts: `{options.get('scorer_use_strength_counts') or {}}` threshold candidates `{options.get('threshold_candidate_context_count') or 0}` context-only `{options.get('context_only_count') or 0}`",
            f"- Concepts: `{options.get('tracked_concepts') or []}`",
            "",
            "| Use | Strength | Seen | Direction | Share | Non-low confidence | Avg abs return | Pattern | Top symbols |",
            "| --- | --- | ---: | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for item in (options.get("memories") or [])[:12]:
        lines.append(
            f"| `{item.get('recommended_use')}` | `{item.get('scorer_use_strength')}` | {item.get('seen')} | `{item.get('dominant_direction')}` | "
            f"{item.get('dominant_direction_share')} | {item.get('non_low_confidence_share')} | {item.get('avg_abs_return')} | `{item.get('pattern_key')}` | `{item.get('top_symbols')}` |"
        )
    if not options.get("memories"):
        lines.append("| - | - | - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 5. Standing Regime Overlay Learning",
            "",
            f"- Available: `{standing.get('available')}` active/easing `{standing.get('active_or_easing_count')}` fresh `{standing.get('fresh_count')}` aging `{standing.get('aging_count')}` resolution-watch `{standing.get('resolution_watch_count')}`",
            f"- Bias counts: `{standing.get('bias_counts') or {}}`",
            f"- Family counts: `{standing.get('family_counts') or {}}`",
            "",
            "| Bias | Status | Freshness | Weight | Family | Assets | Latest evidence |",
            "| --- | --- | --- | ---: | --- | --- | --- |",
        ]
    )
    for item in (standing.get("top_regimes") or [])[:12]:
        lines.append(
            f"| `{item.get('directional_bias')}` | `{item.get('status')}` | `{item.get('freshness_status')}` | "
            f"{item.get('effective_weight')} | `{item.get('family')}` | `{item.get('affected_assets')}` | "
            f"{str(item.get('latest_titles') or [])[:180].replace('|', '/')} |"
        )
    if not standing.get("top_regimes"):
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## 6. Live/Paper Action Gate Learning",
            "",
            f"- Available: `{action.get('available')}` rows `{action.get('source_rows')}` memories `{action.get('memory_count')}` paper candidates `{action.get('paper_entry_candidate_count')}` block/second-pass `{action.get('block_or_second_pass_count')}`",
            "",
            "| Policy | Seen | Hit rate | Avg error | Avg abs return | Pattern | Top symbols |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        ]
    )
    for item in (action.get("memories") or [])[:12]:
        lines.append(
            f"| `{item.get('recommended_action_gate_policy')}` | {item.get('seen')} | {item.get('hit_rate')} | "
            f"{item.get('avg_absolute_error')} | {item.get('avg_abs_return')} | `{item.get('pattern_key')}` | `{item.get('top_symbols')}` |"
        )
    if not action.get("memories"):
        lines.append("| - | - | - | - | - | - | - |")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- This is a read-only/shadow control artifact.",
            "- It does not mutate the live scorer, promote thresholds, create paper orders, submit broker orders, enable transmit, or enable live routing.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_live_learning_control_plane_json(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(resolved, json.dumps(result, indent=2, ensure_ascii=False))
    return resolved


def write_live_learning_control_plane_markdown(path: Path, result: dict[str, Any]) -> Path:
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(resolved, live_learning_control_plane_markdown(result))
    return resolved


def _count_one(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    try:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


def _standing_regime_hot_cache(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    if not _table_exists(conn, "standing_market_regimes"):
        return {
            "version": "m232-standing-regime-cache-v1",
            "available": False,
            "reason": "standing_market_regimes_missing",
            "top_regimes": [],
        }
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT regime_key,family,regime_name,directional_bias,status,region,theme,
                   started_at,last_seen_at,resolved_at,severity_score,confidence,
                   affected_assets_json,evidence_count,latest_titles_json,threshold_profile_json,
                   raw_json,updated_at
              FROM standing_market_regimes
             WHERE status IN ('active','easing')
             ORDER BY
               CASE status WHEN 'active' THEN 1 WHEN 'easing' THEN 2 ELSE 3 END,
               severity_score DESC,last_seen_at DESC,id DESC
             LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    ]
    family_counts: Counter[str] = Counter()
    bias_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    freshness_counts: Counter[str] = Counter()
    top_regimes: list[dict[str, Any]] = []
    for row in rows:
        metrics = standing_regime_runtime_metrics(row)
        family = str(row.get("family") or "unknown")
        bias = str(row.get("directional_bias") or "unknown")
        status = str(row.get("status") or "unknown")
        freshness = str(metrics.get("freshness_status") or "unknown")
        family_counts.update([family])
        bias_counts.update([bias])
        status_counts.update([status])
        freshness_counts.update([freshness])
        assets = _safe_json(row.get("affected_assets_json"), [])
        titles = _safe_json(row.get("latest_titles_json"), [])
        threshold_profile = _safe_json(row.get("threshold_profile_json"), {})
        resolution_watch = status == "easing" or freshness in {"aging", "stale_or_resolved"}
        top_regimes.append(
            {
                "regime_key": row.get("regime_key"),
                "family": family,
                "regime_name": row.get("regime_name"),
                "directional_bias": bias,
                "status": status,
                "region": row.get("region"),
                "theme": row.get("theme"),
                "started_at": row.get("started_at"),
                "last_seen_at": row.get("last_seen_at"),
                "resolved_at": row.get("resolved_at"),
                "evidence_count": int(row.get("evidence_count") or 0),
                "affected_assets": assets[:20] if isinstance(assets, list) else [],
                "latest_titles": titles[-3:] if isinstance(titles, list) else [],
                "freshness_status": freshness,
                "age_days": metrics.get("age_days"),
                "effective_weight": metrics.get("effective_weight"),
                "status_decay": metrics.get("status_decay"),
                "resolution_watch": resolution_watch,
                "threshold_profile": {
                    "positive_regime_news": threshold_profile.get("positive_regime_news"),
                    "negative_regime_news": threshold_profile.get("negative_regime_news"),
                    "neutral_regime_news": threshold_profile.get("neutral_regime_news"),
                }
                if isinstance(threshold_profile, dict)
                else {},
            }
        )
    top_regimes.sort(key=lambda item: float(item.get("effective_weight") or 0.0), reverse=True)
    return {
        "version": "m232-standing-regime-cache-v1",
        "available": True,
        "generated_at": _iso_now(),
        "source_rows": len(rows),
        "active_or_easing_count": len(rows),
        "family_counts": dict(family_counts.most_common(20)),
        "bias_counts": dict(bias_counts),
        "status_counts": dict(status_counts),
        "freshness_counts": dict(freshness_counts),
        "fresh_count": int(freshness_counts.get("fresh_confirmation") or 0) + int(freshness_counts.get("confirmed") or 0),
        "aging_count": int(freshness_counts.get("aging") or 0),
        "resolution_watch_count": sum(1 for item in top_regimes if item.get("resolution_watch")),
        "top_regimes": top_regimes[:25],
        "scorer_use": {
            "risk_on": "can confirm bullish calls for mapped beneficiaries when other desks agree",
            "risk_off": "can haircut bullish calls or confirm bearish calls while active/fresh",
            "easing_or_stale": "use as context only unless fresh evidence re-intensifies the regime",
            "resolution": "normalize thresholds only after confirmation evidence, not a single optimistic headline",
        },
        "safety": {
            "cache_only": True,
            "scorer_threshold_mutated": False,
            "live_threshold_promoted": False,
            "broker_orders_submitted": False,
        },
    }


def _geopolitical_hot_cache(conn: sqlite3.Connection, *, limit: int) -> dict[str, Any]:
    try:
        heatmap = build_geopolitical_heatmap_from_connection(conn, lookback_hours=72.0, limit=max(5, int(limit)))
    except Exception as exc:
        return {
            "version": "m232-geopolitical-hot-cache-v1",
            "available": False,
            "reason": str(exc)[:300],
            "top_buckets": [],
            "hot_bucket_count": 0,
            "safety": {
                "cache_only": True,
                "scorer_threshold_mutated": False,
                "broker_orders_submitted": False,
            },
        }
    top_buckets = heatmap.get("top_buckets") or []
    hot_buckets = [row for row in top_buckets if row.get("heat_color") in {"red", "yellow"}]
    return {
        "version": "m232-geopolitical-hot-cache-v1",
        "available": True,
        "generated_at": heatmap.get("generated_at"),
        "lookback_hours": heatmap.get("lookback_hours"),
        "bucket_count": heatmap.get("bucket_count"),
        "hot_bucket_count": len(hot_buckets),
        "heat_color_counts": heatmap.get("heat_color_counts") or {},
        "top_buckets": top_buckets[:25],
        "scorer_use": {
            "red": "second-pass geopolitical explanation and risk/reaction cross-check before aggressive entries",
            "yellow": "confidence haircut or smaller sizing unless market/economic/options desks confirm",
            "watch": "monitor-only context unless matching event arrives",
        },
        "safety": {
            "cache_only": True,
            "scorer_threshold_mutated": False,
            "live_threshold_promoted": False,
            "broker_orders_submitted": False,
        },
    }


def _m232_source_state(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "checked_at": _iso_now(),
        "emergency_count": _count_one(conn, "SELECT COUNT(*) FROM external_market_signals WHERE source='emergency_breaking_news'") if _table_exists(conn, "external_market_signals") else None,
        "emergency_latest": _count_one(conn, "SELECT MAX(updated_at) FROM external_market_signals WHERE source='emergency_breaking_news'") if _table_exists(conn, "external_market_signals") else None,
        "option_outcome_count": _count_one(conn, "SELECT COUNT(*) FROM option_structure_outcome_logs") if _table_exists(conn, "option_structure_outcome_logs") else None,
        "option_outcome_complete_count": _count_one(conn, "SELECT COUNT(*) FROM option_structure_outcome_logs WHERE price_status='complete'") if _table_exists(conn, "option_structure_outcome_logs") else None,
        "option_outcome_latest": _count_one(conn, "SELECT MAX(updated_at) FROM option_structure_outcome_logs") if _table_exists(conn, "option_structure_outcome_logs") else None,
        "gate_severity_outcome_count": _count_one(conn, "SELECT COUNT(*) FROM m250_5_gate_severity_outcomes") if _table_exists(conn, "m250_5_gate_severity_outcomes") else None,
        "gate_severity_evaluated_count": _count_one(conn, "SELECT COUNT(*) FROM m250_5_gate_severity_outcomes WHERE status='evaluated'") if _table_exists(conn, "m250_5_gate_severity_outcomes") else None,
        "gate_severity_latest": _count_one(conn, "SELECT MAX(updated_at) FROM m250_5_gate_severity_outcomes") if _table_exists(conn, "m250_5_gate_severity_outcomes") else None,
        "standing_regime_count": _count_one(conn, "SELECT COUNT(*) FROM standing_market_regimes WHERE status IN ('active','easing')") if _table_exists(conn, "standing_market_regimes") else None,
        "standing_regime_latest": _count_one(conn, "SELECT MAX(updated_at) FROM standing_market_regimes") if _table_exists(conn, "standing_market_regimes") else None,
        "standing_regime_outcome_count": _count_one(conn, "SELECT COUNT(*) FROM standing_regime_outcomes") if _table_exists(conn, "standing_regime_outcomes") else None,
        "standing_regime_outcome_evaluated_count": _count_one(conn, "SELECT COUNT(*) FROM standing_regime_outcomes WHERE status='evaluated'") if _table_exists(conn, "standing_regime_outcomes") else None,
        "standing_regime_outcome_latest": _count_one(conn, "SELECT MAX(updated_at) FROM standing_regime_outcomes") if _table_exists(conn, "standing_regime_outcomes") else None,
        "geopolitical_outcome_count": _count_one(conn, "SELECT COUNT(*) FROM geopolitical_signal_outcomes") if _table_exists(conn, "geopolitical_signal_outcomes") else None,
        "geopolitical_outcome_evaluated_count": _count_one(conn, "SELECT COUNT(*) FROM geopolitical_signal_outcomes WHERE status='evaluated'") if _table_exists(conn, "geopolitical_signal_outcomes") else None,
        "geopolitical_outcome_latest": _count_one(conn, "SELECT MAX(updated_at) FROM geopolitical_signal_outcomes") if _table_exists(conn, "geopolitical_signal_outcomes") else None,
        "geopolitical_adaptive_map_count": _count_one(conn, "SELECT COUNT(*) FROM geopolitical_adaptive_asset_map") if _table_exists(conn, "geopolitical_adaptive_asset_map") else None,
        "geopolitical_adaptive_map_latest": _count_one(conn, "SELECT MAX(updated_at) FROM geopolitical_adaptive_asset_map") if _table_exists(conn, "geopolitical_adaptive_asset_map") else None,
        "m223_prediction_count": _count_one(conn, "SELECT COUNT(*) FROM m223_live_learning_predictions") if _table_exists(conn, "m223_live_learning_predictions") else None,
        "m223_outcome_count": _count_one(conn, "SELECT COUNT(*) FROM m223_live_learning_outcomes") if _table_exists(conn, "m223_live_learning_outcomes") else None,
        "m223_evaluated_count": _count_one(conn, "SELECT COUNT(*) FROM m223_live_learning_outcomes WHERE status='evaluated'") if _table_exists(conn, "m223_live_learning_outcomes") else None,
        "m223_latest_prediction_id": _count_one(conn, "SELECT MAX(id) FROM m223_live_learning_predictions") if _table_exists(conn, "m223_live_learning_predictions") else None,
        "m223_latest_outcome_id": _count_one(conn, "SELECT MAX(id) FROM m223_live_learning_outcomes") if _table_exists(conn, "m223_live_learning_outcomes") else None,
    }


def _m232_adaptive_schedule(
    *,
    config: LiveLearningControlPlaneConfig,
    previous_state: dict[str, Any] | None,
    current_state: dict[str, Any],
    seconds_until_full_due: float,
) -> dict[str, Any]:
    if not config.adaptive_enabled:
        return {
            "adaptive_enabled": False,
            "tier": "fixed",
            "next_interval_seconds": max(0.0, float(config.interval_seconds)),
            "changed_keys": [],
            "full_report_due": seconds_until_full_due <= 0,
        }
    comparable_keys = [
        "emergency_count",
        "emergency_latest",
        "option_outcome_count",
        "option_outcome_complete_count",
        "option_outcome_latest",
        "gate_severity_outcome_count",
        "gate_severity_evaluated_count",
        "gate_severity_latest",
        "standing_regime_count",
        "standing_regime_latest",
        "standing_regime_outcome_count",
        "standing_regime_outcome_evaluated_count",
        "standing_regime_outcome_latest",
        "geopolitical_outcome_count",
        "geopolitical_outcome_evaluated_count",
        "geopolitical_outcome_latest",
        "geopolitical_adaptive_map_count",
        "geopolitical_adaptive_map_latest",
        "m223_prediction_count",
        "m223_outcome_count",
        "m223_evaluated_count",
        "m223_latest_prediction_id",
        "m223_latest_outcome_id",
    ]
    changed = []
    if previous_state is None:
        changed = comparable_keys[:]
    else:
        changed = [key for key in comparable_keys if previous_state.get(key) != current_state.get(key)]
    full_due = seconds_until_full_due <= 0
    if full_due:
        tier = "full_due"
        interval = float(config.hot_min_interval_seconds)
    elif any(key.startswith("emergency") for key in changed):
        tier = "hot_emergency"
        interval = float(config.hot_min_interval_seconds)
    elif any(key.startswith(("m223", "option", "gate_severity", "standing_regime", "geopolitical")) for key in changed):
        tier = "warm_learning_delta"
        interval = float(config.hot_normal_interval_seconds)
    else:
        tier = "idle"
        interval = float(config.hot_max_interval_seconds)
    interval = max(float(config.hot_min_interval_seconds), min(float(config.hot_max_interval_seconds), interval))
    return {
        "adaptive_enabled": True,
        "tier": tier,
        "next_interval_seconds": interval,
        "changed_keys": changed,
        "full_report_due": full_due,
        "seconds_until_full_due": round(max(0.0, seconds_until_full_due), 3),
        "source_state": current_state,
    }


def _build_live_learning_hot_cache(config: LiveLearningControlPlaneConfig, *, source_state: dict[str, Any], schedule: dict[str, Any]) -> dict[str, Any]:
    m226 = _read_json(config.threshold_candidate_evaluator_json_path)
    m250 = _read_json(config.limited_auto_readiness_json_path)
    limit = max(25, int(config.hot_limit))
    generated_at = _iso_now()
    max_consumer_age_seconds = max(float(config.hot_max_interval_seconds) * 2.0, 120.0)
    with _connect(config.database_path) as conn:
        emergency = _emergency_reflex_memory(conn, limit=min(limit, int(config.emergency_limit)))
        option_learning = _option_structure_learning(conn, limit=min(limit, int(config.option_outcome_limit)))
        action_learning = _action_gate_learning(conn, limit=min(limit, int(config.action_gate_limit)))
        standing_regimes = _standing_regime_hot_cache(conn, limit=min(limit, 250))
        geopolitical = _geopolitical_hot_cache(conn, limit=min(limit, 250))
    application = _promotion_gate_application(m226, m250)
    reflex_memories = [
        item
        for item in emergency.get("memories") or []
        if item.get("recommended_reflex_action") == "reflex_candidate"
    ][:25]
    if not reflex_memories:
        reflex_memories = (emergency.get("memories") or [])[:25]
    option_memories = [
        item
        for item in option_learning.get("memories") or []
        if item.get("scorer_use_strength") in {"strong", "medium"}
    ][:25]
    if not option_memories:
        option_memories = (option_learning.get("memories") or [])[:25]
    action_memories = [
        item
        for item in action_learning.get("memories") or []
        if item.get("recommended_action_gate_policy") in {"paper_entry_candidate", "block_or_second_pass"}
    ][:25]
    if not action_memories:
        action_memories = (action_learning.get("memories") or [])[:25]
    return {
        "m232_hot_cache_version": "m232-hot-cache-v1",
        "generated_at": generated_at,
        "max_consumer_age_seconds": max_consumer_age_seconds,
        "mode": "adaptive_lightweight_cache",
        "source_state": source_state,
        "adaptive_schedule": schedule,
        "standing_regime_cache": standing_regimes,
        "geopolitical_hot_cache": geopolitical,
        "scorer_overlay_cache": {
            "version": "m232-scorer-overlay-cache-v1",
            "generated_at": generated_at,
            "parent_hot_cache_generated_at": generated_at,
            "max_consumer_age_seconds": max_consumer_age_seconds,
            "application_mode": application.get("application_mode"),
            "paper_scorer_ready": application.get("paper_scorer_ready"),
            "conditional_overlay_available": application.get("conditional_overlay_available"),
            "waitlist_count": application.get("waitlist_count"),
            "watchlist_count": application.get("watchlist_count"),
            "blocked_count": application.get("blocked_count"),
            "hooks": application.get("scorer_evidence_packet_hooks") or [],
            "policies": application.get("scorer_threshold_application_policies") or [],
            "gate_severity_overlay": application.get("gate_severity_overlay") or {},
            "standing_regime_cache": standing_regimes,
            "geopolitical_hot_cache": geopolitical,
            "withhold_reasons": {
                "watchlist": application.get("watchlist_detail_reason_counts") or {},
                "blocked": application.get("blocked_detail_reason_counts") or {},
            },
        },
        "reflex_hot_cache": {
            "version": "m232-reflex-hot-cache-v1",
            "generated_at": generated_at,
            "parent_hot_cache_generated_at": generated_at,
            "max_consumer_age_seconds": max_consumer_age_seconds,
            "source_rows": emergency.get("source_rows"),
            "memory_count": emergency.get("memory_count"),
            "reflex_candidate_count": emergency.get("reflex_candidate_count"),
            "top_assets": emergency.get("top_assets") or {},
            "memories": reflex_memories,
        },
        "option_structure_cache": {
            "version": "m232-option-structure-cache-v1",
            "generated_at": generated_at,
            "parent_hot_cache_generated_at": generated_at,
            "max_consumer_age_seconds": max_consumer_age_seconds,
            "source_rows": option_learning.get("source_rows"),
            "complete_rows": option_learning.get("complete_rows"),
            "memory_count": option_learning.get("memory_count"),
            "scorer_use_strength_counts": option_learning.get("scorer_use_strength_counts") or {},
            "threshold_candidate_context_count": option_learning.get("threshold_candidate_context_count") or 0,
            "context_only_count": option_learning.get("context_only_count") or 0,
            "memories": option_memories,
        },
        "action_gate_cache": {
            "version": "m232-action-gate-cache-v1",
            "generated_at": generated_at,
            "parent_hot_cache_generated_at": generated_at,
            "max_consumer_age_seconds": max_consumer_age_seconds,
            "source_rows": action_learning.get("source_rows"),
            "memory_count": action_learning.get("memory_count"),
            "paper_entry_candidate_count": action_learning.get("paper_entry_candidate_count"),
            "block_or_second_pass_count": action_learning.get("block_or_second_pass_count"),
            "gate_severity_overlay": application.get("gate_severity_overlay") or {},
            "geopolitical_hot_cache": {
                "available": geopolitical.get("available"),
                "hot_bucket_count": geopolitical.get("hot_bucket_count"),
                "heat_color_counts": geopolitical.get("heat_color_counts") or {},
                "top_buckets": (geopolitical.get("top_buckets") or [])[:5],
            },
            "standing_regime_cache": {
                "available": standing_regimes.get("available"),
                "active_or_easing_count": standing_regimes.get("active_or_easing_count"),
                "bias_counts": standing_regimes.get("bias_counts") or {},
                "resolution_watch_count": standing_regimes.get("resolution_watch_count"),
            },
            "memories": action_memories,
        },
        "safety": {
            "hot_path_cache_only": True,
            "db_full_scan_required_by_hot_path": False,
            "live_scorer_mutated": False,
            "threshold_table_mutated": False,
            "paper_orders_created": False,
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
        },
    }


def _write_json(path: Path | None, payload: dict[str, Any]) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(resolved, json.dumps(payload, indent=2, ensure_ascii=False))
    return resolved


def write_live_learning_hot_caches(config: LiveLearningControlPlaneConfig, hot_cache: dict[str, Any]) -> dict[str, str]:
    written: dict[str, str] = {}
    mapping = {
        "hot_cache": (config.hot_cache_json_path, hot_cache),
        "reflex_hot_cache": (config.reflex_hot_cache_json_path, hot_cache.get("reflex_hot_cache") or {}),
        "scorer_overlay_cache": (config.scorer_overlay_cache_json_path, hot_cache.get("scorer_overlay_cache") or {}),
        "option_structure_cache": (config.option_structure_cache_json_path, hot_cache.get("option_structure_cache") or {}),
        "action_gate_cache": (config.action_gate_cache_json_path, hot_cache.get("action_gate_cache") or {}),
    }
    for key, (path, payload) in mapping.items():
        resolved = _write_json(path, payload)
        if resolved is not None:
            written[key] = str(resolved)
    return written


def _refresh_context_learning_artifacts(config: LiveLearningControlPlaneConfig) -> dict[str, Any]:
    if not config.refresh_context_learning:
        return {"enabled": False}
    started_at = _iso_now()
    db = AnalysisDatabase(config.database_path)
    result: dict[str, Any] = {
        "enabled": True,
        "started_at": started_at,
        "artifacts": {},
        "errors": [],
    }
    try:
        geopolitical = build_geopolitical_intelligence_report(
            db,
            lookback_hours=config.context_refresh_lookback_hours,
            horizons_minutes=config.context_refresh_horizons_minutes,
            signal_limit=config.context_refresh_signal_limit,
            max_assets_per_signal=config.context_refresh_max_assets_per_signal,
            max_outcome_lag_minutes=config.context_refresh_max_outcome_lag_minutes,
            evaluation_limit=config.context_refresh_evaluation_limit,
        )
        if config.geopolitical_intelligence_json_path is not None:
            result["artifacts"]["geopolitical_json"] = str(write_geopolitical_intelligence_json(config.geopolitical_intelligence_json_path, geopolitical))
        if config.geopolitical_intelligence_output_path is not None:
            result["artifacts"]["geopolitical_markdown"] = str(write_geopolitical_intelligence_markdown(config.geopolitical_intelligence_output_path, geopolitical))
        result["geopolitical"] = {
            "candidate_signal_count": geopolitical.get("candidate_signal_count"),
            "source_signal_count": geopolitical.get("source_signal_count"),
            "recorded_count": geopolitical.get("recorded_count"),
            "duplicate_skipped_count": geopolitical.get("duplicate_skipped_count"),
            "evaluated_count": geopolitical.get("evaluated_count"),
            "signal_scope_counts": geopolitical.get("signal_scope_counts") or {},
            "status_counts": geopolitical.get("status_counts") or {},
        }
    except Exception as exc:
        result["errors"].append({"component": "geopolitical_intelligence", "error": str(exc)[:500]})
    try:
        standing = build_standing_regime_outcome_report(
            db,
            symbols=config.context_refresh_symbols,
            horizons_minutes=config.context_refresh_horizons_minutes,
            max_outcome_lag_minutes=config.context_refresh_max_outcome_lag_minutes,
            evaluation_limit=config.context_refresh_evaluation_limit,
        )
        if config.standing_regime_outcomes_json_path is not None:
            result["artifacts"]["standing_regime_outcomes_json"] = str(write_standing_regime_outcome_json(config.standing_regime_outcomes_json_path, standing))
        if config.standing_regime_outcomes_output_path is not None:
            result["artifacts"]["standing_regime_outcomes_markdown"] = str(write_standing_regime_outcome_markdown(config.standing_regime_outcomes_output_path, standing))
        result["standing_regimes"] = {
            "recorded_count": standing.get("recorded_count"),
            "evaluated_count": standing.get("evaluated_count"),
            "status_counts": standing.get("status_counts") or {},
        }
    except Exception as exc:
        result["errors"].append({"component": "standing_regime_outcomes", "error": str(exc)[:500]})
    result["completed_at"] = _iso_now()
    result["ok"] = not result["errors"]
    return result


def _main() -> int:
    parser = argparse.ArgumentParser(description="Build read-only M232 live learning control-plane artifact")
    parser.add_argument("--database-path", type=Path, default=Path("market_events.db"))
    parser.add_argument("--live-scorer-learning-json", type=Path, default=Path("exports/m223_live_scorer_learning.json"))
    parser.add_argument("--threshold-candidate-evaluator-json", type=Path, default=Path("exports/m226_threshold_candidate_evaluator_summary.json"))
    parser.add_argument("--limited-auto-readiness-json", type=Path, default=Path("exports/m250_limited_auto_trading_readiness.json"))
    parser.add_argument("--option-outcome-limit", type=int, default=5000)
    parser.add_argument("--emergency-limit", type=int, default=5000)
    parser.add_argument("--action-gate-limit", type=int, default=5000)
    parser.add_argument("--max-ticks", type=int, default=1, help="0 means run forever")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    parser.add_argument("--sleep-between-ticks", action="store_true")
    parser.add_argument("--adaptive", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--hot-limit", type=int, default=500)
    parser.add_argument("--hot-min-interval-seconds", type=float, default=5.0)
    parser.add_argument("--hot-normal-interval-seconds", type=float, default=15.0)
    parser.add_argument("--hot-max-interval-seconds", type=float, default=60.0)
    parser.add_argument("--full-report-interval-seconds", type=float, default=300.0)
    parser.add_argument("--hot-cache-json-path", type=Path, default=Path("exports/m232_hot_cache.json"))
    parser.add_argument("--reflex-hot-cache-json-path", type=Path, default=Path("exports/m232_reflex_hot_cache.json"))
    parser.add_argument("--scorer-overlay-cache-json-path", type=Path, default=Path("exports/m232_scorer_overlay_cache.json"))
    parser.add_argument("--option-structure-cache-json-path", type=Path, default=Path("exports/m232_option_structure_cache.json"))
    parser.add_argument("--action-gate-cache-json-path", type=Path, default=Path("exports/m232_action_gate_cache.json"))
    parser.add_argument("--refresh-context-learning", action="store_true", help="Refresh Geo/standing-regime learning artifacts on full M232 cycles")
    parser.add_argument("--context-refresh-lookback-hours", type=float, default=168.0)
    parser.add_argument("--context-refresh-signal-limit", type=int, default=2000)
    parser.add_argument("--context-refresh-horizons-minutes", default="5,15,30,390")
    parser.add_argument("--context-refresh-max-assets-per-signal", type=int, default=6)
    parser.add_argument("--context-refresh-max-outcome-lag-minutes", type=float, default=1440.0)
    parser.add_argument("--context-refresh-evaluation-limit", type=int, default=5000)
    parser.add_argument("--context-refresh-symbols", default="SPY,QQQ,IWM,DIA,SMH,SOXX,USO,XLE,GLD,VIX")
    parser.add_argument("--geopolitical-intelligence-json-path", type=Path, default=Path("exports/geopolitical_intelligence.json"))
    parser.add_argument("--geopolitical-intelligence-output", type=Path, default=Path("exports/geopolitical_intelligence.md"))
    parser.add_argument("--standing-regime-outcomes-json-path", type=Path, default=Path("exports/standing_regime_outcomes.json"))
    parser.add_argument("--standing-regime-outcomes-output", type=Path, default=Path("exports/standing_regime_outcomes.md"))
    parser.add_argument("--lock-path", type=Path, default=Path("locks/live_learning_control_plane.lock"))
    parser.add_argument("--no-lock", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("exports/m232_live_learning_control_plane.md"))
    parser.add_argument("--json-output-path", type=Path, default=Path("exports/m232_live_learning_control_plane.json"))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    config = LiveLearningControlPlaneConfig(
        database_path=args.database_path,
        live_scorer_learning_json_path=args.live_scorer_learning_json,
        threshold_candidate_evaluator_json_path=args.threshold_candidate_evaluator_json,
        limited_auto_readiness_json_path=args.limited_auto_readiness_json,
        option_outcome_limit=args.option_outcome_limit,
        emergency_limit=args.emergency_limit,
        action_gate_limit=args.action_gate_limit,
        max_ticks=args.max_ticks,
        interval_seconds=args.interval_seconds,
        sleep_between_ticks=args.sleep_between_ticks,
        adaptive_enabled=bool(args.adaptive),
        hot_limit=args.hot_limit,
        hot_min_interval_seconds=args.hot_min_interval_seconds,
        hot_normal_interval_seconds=args.hot_normal_interval_seconds,
        hot_max_interval_seconds=args.hot_max_interval_seconds,
        full_report_interval_seconds=args.full_report_interval_seconds,
        hot_cache_json_path=args.hot_cache_json_path,
        reflex_hot_cache_json_path=args.reflex_hot_cache_json_path,
        scorer_overlay_cache_json_path=args.scorer_overlay_cache_json_path,
        option_structure_cache_json_path=args.option_structure_cache_json_path,
        action_gate_cache_json_path=args.action_gate_cache_json_path,
        refresh_context_learning=bool(args.refresh_context_learning),
        context_refresh_lookback_hours=args.context_refresh_lookback_hours,
        context_refresh_signal_limit=args.context_refresh_signal_limit,
        context_refresh_horizons_minutes=args.context_refresh_horizons_minutes,
        context_refresh_max_assets_per_signal=args.context_refresh_max_assets_per_signal,
        context_refresh_max_outcome_lag_minutes=args.context_refresh_max_outcome_lag_minutes,
        context_refresh_evaluation_limit=args.context_refresh_evaluation_limit,
        context_refresh_symbols=args.context_refresh_symbols,
        geopolitical_intelligence_json_path=args.geopolitical_intelligence_json_path,
        geopolitical_intelligence_output_path=args.geopolitical_intelligence_output,
        standing_regime_outcomes_json_path=args.standing_regime_outcomes_json_path,
        standing_regime_outcomes_output_path=args.standing_regime_outcomes_output,
        lock_path=None if args.no_lock else args.lock_path,
        output_path=args.output,
        json_output_path=args.json_output_path,
    )
    if config.lock_path is not None:
        try:
            with SingleInstanceLock("live_learning_control_plane", config.lock_path):
                result = _run_control_plane_loop(config)
        except AlreadyRunningError as exc:
            payload = dict(exc.payload)
            payload["live_learning_control_plane_version"] = LIVE_LEARNING_CONTROL_PLANE_VERSION
            payload["status"] = "already_running_blocked"
            if args.json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(f"M232 already running with pid {payload.get('pid')}; lock {payload.get('lock_path')}")
            return 0
    else:
        result = _run_control_plane_loop(config)
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(live_learning_control_plane_markdown(result))
    return 0


def _run_control_plane_loop(config: LiveLearningControlPlaneConfig) -> dict[str, Any]:
    tick = 0
    result: dict[str, Any] = {}
    previous_source_state: dict[str, Any] | None = None
    next_full_due_at = 0.0
    while True:
        tick += 1
        now_monotonic = time.monotonic()
        with _connect(config.database_path) as conn:
            source_state = _m232_source_state(conn)
        schedule = _m232_adaptive_schedule(
            config=config,
            previous_state=previous_source_state,
            current_state=source_state,
            seconds_until_full_due=next_full_due_at - now_monotonic,
        )
        if tick == 1 or schedule.get("full_report_due"):
            context_refresh = _refresh_context_learning_artifacts(config)
            result = build_live_learning_control_plane(config)
            result["context_learning_refresh"] = context_refresh
            result["tick_index"] = tick
            result["adaptive_schedule"] = schedule
            if config.json_output_path:
                write_live_learning_control_plane_json(config.json_output_path, result)
            if config.output_path:
                write_live_learning_control_plane_markdown(config.output_path, result)
            next_full_due_at = now_monotonic + max(float(config.full_report_interval_seconds), float(config.hot_min_interval_seconds))
        hot_cache = _build_live_learning_hot_cache(config, source_state=source_state, schedule=schedule)
        hot_cache["tick_index"] = tick
        hot_cache["full_report_latest_generated_at"] = result.get("generated_at")
        hot_cache["full_report_next_due_in_seconds"] = round(max(0.0, next_full_due_at - time.monotonic()), 3)
        if result.get("context_learning_refresh"):
            hot_cache["context_learning_refresh"] = result.get("context_learning_refresh")
        write_live_learning_hot_caches(config, hot_cache)
        previous_source_state = source_state
        if int(config.max_ticks) > 0 and tick >= int(config.max_ticks):
            break
        if config.sleep_between_ticks:
            sleep_seconds = float(schedule.get("next_interval_seconds") if schedule.get("next_interval_seconds") is not None else config.interval_seconds)
            time.sleep(max(0.0, sleep_seconds))
        else:
            break
    return result


if __name__ == "__main__":
    raise SystemExit(_main())
