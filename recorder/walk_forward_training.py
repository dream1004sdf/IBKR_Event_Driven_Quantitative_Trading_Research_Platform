from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .evidence_packet import build_overlay_evidence_packet


WALK_FORWARD_TRAINING_VERSION = "walk-forward-training-sim-v1"


def _parse_dt(value: Any) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _signed_actual_excess(row: dict[str, Any]) -> float | None:
    direction = str(row.get("predicted_direction") or "").lower()
    actual = _safe_float(row.get("actual_excess_return"))
    if actual is None or direction not in {"up", "down"}:
        return None
    return actual if direction == "up" else -actual


def _direction_hit(row: dict[str, Any]) -> int | None:
    value = row.get("direction_hit")
    if value is None:
        predicted = str(row.get("predicted_direction") or "").lower()
        actual = str(row.get("actual_direction") or "").lower()
        if predicted in {"up", "down"} and actual in {"up", "down", "flat"}:
            return int(predicted == actual)
        return None
    return int(value)


def _gap(row: dict[str, Any]) -> dict[str, Any]:
    expected = _safe_float(row.get("expected_return"))
    actual = _safe_float(row.get("actual_excess_return"))
    signed_actual = _signed_actual_excess(row)
    direction_hit = _direction_hit(row)
    abs_error = None
    if expected is not None and actual is not None:
        abs_error = abs(expected - actual)
    if direction_hit == 0:
        gap_type = "direction_miss"
    elif signed_actual is not None and signed_actual < 0:
        gap_type = "signed_return_negative_hit_or_flat"
    elif abs_error is not None and abs_error >= 0.03:
        gap_type = "large_return_error"
    else:
        gap_type = "small_gap_or_hit"
    return {
        "direction_hit": direction_hit,
        "expected_return": expected,
        "actual_excess_return": actual,
        "signed_actual_excess_return": signed_actual,
        "absolute_excess_error": abs_error,
        "gap_type": gap_type,
    }


def _relation_to_prediction(predicted: str, bias: str) -> str:
    predicted = predicted.lower()
    bias = bias.lower()
    if predicted == "up":
        if bias == "risk_on":
            return "confirm"
        if bias == "risk_off":
            return "conflict"
    if predicted == "down":
        if bias == "risk_off":
            return "confirm"
        if bias == "risk_on":
            return "conflict"
    return "neutral"


def _packet_tracks(row: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    tracks = ["market_news_primary"]
    overlay = packet.get("overlay") or {}
    standing = packet.get("standing_regime_overlay") or {}
    economic = packet.get("economic_indicator_desk") or {}
    if bool(overlay.get("emergency")) or any(
        bool(item.get("emergency")) for item in (overlay.get("top_signals") or []) if isinstance(item, dict)
    ):
        tracks.append("emergency_reflex")
    if str(overlay.get("overlay_state") or "normal") != "normal" or int(overlay.get("matched_signal_count") or 0) > 0:
        tracks.append("short_geopolitical")
    if int(standing.get("matched_regime_count") or 0) > 0:
        tracks.append("standing_regime")
    if str(economic.get("risk_bias") or "") in {"macro_risk_off", "macro_mixed"}:
        tracks.append("economic_macro")
    return list(dict.fromkeys(tracks))


def _pattern_keys(row: dict[str, Any], packet: dict[str, Any]) -> list[str]:
    symbol = str(row.get("symbol") or "").upper() or "UNKNOWN"
    predicted = str(row.get("predicted_direction") or "unknown").lower()
    overlay = packet.get("overlay") or {}
    standing = packet.get("standing_regime_overlay") or {}
    economic = packet.get("economic_indicator_desk") or {}
    keys = [
        f"market_news_primary|symbol={symbol}|pred={predicted}",
    ]
    overlay_state = str(overlay.get("overlay_state") or "normal")
    overlay_action = str(overlay.get("overlay_action") or "none")
    if overlay_state != "normal" or int(overlay.get("matched_signal_count") or 0) > 0:
        keys.append(f"short_geopolitical|state={overlay_state}|action={overlay_action}|pred={predicted}")
    if bool(overlay.get("emergency")) or any(
        bool(item.get("emergency")) for item in (overlay.get("top_signals") or []) if isinstance(item, dict)
    ):
        urgency = str(overlay.get("urgency") or "unknown")
        keys.append(f"emergency_reflex|urgency={urgency}|state={overlay_state}|pred={predicted}")
    for regime in (standing.get("active_regimes") or []):
        if not isinstance(regime, dict):
            continue
        family = str(regime.get("family") or "")
        bias = str(regime.get("directional_bias") or "")
        if family:
            relation = _relation_to_prediction(predicted, bias)
            keys.append(f"standing_regime|family={family}|relation={relation}|pred={predicted}")
    risk_bias = str(economic.get("risk_bias") or "")
    if risk_bias in {"macro_risk_off", "macro_mixed"}:
        keys.append(f"economic_macro|risk_bias={risk_bias}|pred={predicted}")
    return list(dict.fromkeys(keys))


def _prefix(key: str) -> str:
    return key.split("|", 1)[0]


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    directional = [row for row in rows if row.get("direction_hit") is not None]
    hits = sum(int(row["direction_hit"]) for row in directional)
    signed = [float(row["signed_actual_excess_return"]) for row in directional if row.get("signed_actual_excess_return") is not None]
    abs_errors = [float(row["absolute_excess_error"]) for row in directional if row.get("absolute_excess_error") is not None]
    return {
        "rows": len(rows),
        "directional_rows": len(directional),
        "directional_hits": hits,
        "directional_accuracy": hits / len(directional) if directional else None,
        "avg_signed_excess_return": sum(signed) / len(signed) if signed else None,
        "avg_absolute_excess_error": sum(abs_errors) / len(abs_errors) if abs_errors else None,
    }


def walk_forward_learning_simulation(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    min_pattern_observations: int = 8,
    weak_hit_rate: float = 0.45,
    packet_builder: Callable[[dict[str, Any], list[dict[str, Any]], Any], dict[str, Any]] = build_overlay_evidence_packet,
) -> dict[str, Any]:
    ordered = sorted(rows, key=lambda row: (_parse_dt(row.get("replay_timestamp")), int(row.get("outcome_id") or 0)))
    memory: dict[str, dict[str, Any]] = {}
    simulated_rows: list[dict[str, Any]] = []
    lessons: list[dict[str, Any]] = []
    track_counter: Counter[str] = Counter()
    gap_counter: Counter[str] = Counter()

    for index, row in enumerate(ordered, start=1):
        packet = packet_builder(row, external_signals, db)
        keys = _pattern_keys(row, packet)
        tracks = _packet_tracks(row, packet)
        prior_candidates = []
        for key in keys:
            stats = memory.get(key, {"seen": 0, "hits": 0})
            seen = int(stats.get("seen") or 0)
            hits = int(stats.get("hits") or 0)
            hit_rate = hits / seen if seen else None
            if seen >= int(min_pattern_observations):
                prior_candidates.append({"key": key, "seen": seen, "hit_rate": hit_rate})
        weak_patterns = [
            item for item in prior_candidates if item["hit_rate"] is not None and item["hit_rate"] < float(weak_hit_rate)
        ]
        memory_action = "observe_only_memory_gate" if weak_patterns else "directional_signal"
        gap = _gap(row)
        direction_hit = gap["direction_hit"]
        simulated = {
            **gap,
            "index": index,
            "symbol": str(row.get("symbol") or ""),
            "horizon": str(row.get("horizon") or ""),
            "replay_timestamp": str(row.get("replay_timestamp") or ""),
            "predicted_direction": str(row.get("predicted_direction") or ""),
            "actual_direction": str(row.get("actual_direction") or ""),
            "tracks": tracks,
            "pattern_keys": keys,
            "memory_action": memory_action,
            "weak_patterns": weak_patterns[:5],
        }
        simulated_rows.append(simulated)
        for track in tracks:
            track_counter[track] += 1
        gap_counter[str(gap["gap_type"])] += 1
        if gap["gap_type"] != "small_gap_or_hit":
            lessons.append(simulated)

        if direction_hit is not None:
            for key in keys:
                stats = memory.setdefault(key, {"seen": 0, "hits": 0, "gap_types": Counter()})
                stats["seen"] = int(stats.get("seen") or 0) + 1
                stats["hits"] = int(stats.get("hits") or 0) + int(direction_hit)
                if isinstance(stats.get("gap_types"), Counter):
                    stats["gap_types"][str(gap["gap_type"])] += 1

    tradeable = [row for row in simulated_rows if row["memory_action"] != "observe_only_memory_gate"]
    observe_only = [row for row in simulated_rows if row["memory_action"] == "observe_only_memory_gate"]
    learned_patterns = []
    for key, stats in memory.items():
        seen = int(stats.get("seen") or 0)
        if seen < int(min_pattern_observations):
            continue
        hits = int(stats.get("hits") or 0)
        hit_rate = hits / seen if seen else None
        learned_patterns.append(
            {
                "pattern_key": key,
                "track": _prefix(key),
                "seen": seen,
                "hits": hits,
                "hit_rate": hit_rate,
                "recommended_action": "observe_only_memory_gate" if hit_rate is not None and hit_rate < float(weak_hit_rate) else "preserve",
                "gap_types": dict(stats.get("gap_types") or {}),
            }
        )
    learned_patterns.sort(key=lambda item: (item["recommended_action"] != "observe_only_memory_gate", item["hit_rate"] or 0.0, -item["seen"], item["pattern_key"]))
    top_lessons = sorted(
        lessons,
        key=lambda item: (
            item["direction_hit"] == 1,
            -float(item["absolute_excess_error"] or 0.0),
            item["replay_timestamp"],
        ),
    )[:20]
    return {
        "version": WALK_FORWARD_TRAINING_VERSION,
        "rows": len(simulated_rows),
        "signals": len(external_signals),
        "min_pattern_observations": int(min_pattern_observations),
        "weak_hit_rate": float(weak_hit_rate),
        "all_rows": _metrics(simulated_rows),
        "tradeable_after_memory": _metrics(tradeable),
        "observe_only_after_memory": _metrics(observe_only),
        "memory_gate_coverage": (
            _metrics(tradeable)["directional_rows"] / _metrics(simulated_rows)["directional_rows"]
            if _metrics(simulated_rows)["directional_rows"]
            else None
        ),
        "track_counts": dict(track_counter),
        "gap_counts": dict(gap_counter),
        "learned_patterns": learned_patterns[:80],
        "top_lessons": top_lessons,
        "curriculum": [
            {
                "stage": "market_news_primary",
                "goal": "Treat one-line emergency news and ordinary market-news articles as the primary prediction stream.",
            },
            {
                "stage": "short_geopolitical",
                "goal": "Predict immediate geopolitical market impact and compare it with realized market reaction.",
            },
            {
                "stage": "standing_regime",
                "goal": "Detect when short geopolitical/news patterns evolve into long-running regimes and alter thresholds for intensifying/easing/resolution events.",
            },
            {
                "stage": "emergency_reflex",
                "goal": "Convert repeated emergency-news lessons into fast deterministic reflex gates that can act without calling the LLM.",
            },
        ],
    }


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def _fmt_signed_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2%}"


def walk_forward_learning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Learning Simulation Report",
) -> str:
    filters = filters or {}
    all_rows = result["all_rows"]
    tradeable = result["tradeable_after_memory"]
    observe_only = result["observe_only_after_memory"]
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{result.get('version')}`",
        f"- Rows: `{result.get('rows')}`",
        f"- External signals available: `{result.get('signals')}`",
        f"- Min pattern observations before memory acts: `{result.get('min_pattern_observations')}`",
        f"- Weak-hit-rate threshold: `{_fmt_pct(result.get('weak_hit_rate'))}`",
        f"- All directional accuracy: `{all_rows['directional_hits']}/{all_rows['directional_rows']} = {_fmt_pct(all_rows['directional_accuracy'])}`",
        f"- Memory-gated tradeable accuracy: `{tradeable['directional_hits']}/{tradeable['directional_rows']} = {_fmt_pct(tradeable['directional_accuracy'])}`",
        f"- Memory-gated observe-only accuracy: `{observe_only['directional_hits']}/{observe_only['directional_rows']} = {_fmt_pct(observe_only['directional_accuracy'])}`",
        f"- Directional coverage after memory gate: `{_fmt_pct(result.get('memory_gate_coverage'))}`",
        "- Leakage policy: rows are processed strictly by replay timestamp; a pattern can affect only future rows after its own prior outcomes have been observed.",
        "- Safety: simulation/report only; no OpenAI call, IBKR account query, portfolio access, order action, or scorer mutation.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(["", "## Curriculum mapping", "", "| Stage | Goal |", "| --- | --- |"])
    for item in result.get("curriculum") or []:
        lines.append(f"| {item.get('stage')} | {str(item.get('goal') or '').replace('|', '/')} |")
    lines.extend(["", "## Track counts", "", "| Track | Rows |", "| --- | ---: |"])
    for key, value in sorted((result.get("track_counts") or {}).items()):
        lines.append(f"| {key} | {value} |")
    lines.extend(["", "## Gap counts", "", "| Gap type | Rows |", "| --- | ---: |"])
    for key, value in sorted((result.get("gap_counts") or {}).items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## Learned memory patterns",
            "",
            "| Action | Track | Seen | Hit rate | Pattern | Gap types |",
            "| --- | --- | ---: | ---: | --- | --- |",
        ]
    )
    patterns = result.get("learned_patterns") or []
    if not patterns:
        lines.append("| - | - | 0 | - | - | - |")
    for item in patterns[:30]:
        gaps = ", ".join(f"{key}:{value}" for key, value in sorted((item.get("gap_types") or {}).items()))
        lines.append(
            f"| {item.get('recommended_action')} | {item.get('track')} | {item.get('seen')} "
            f"| {_fmt_pct(item.get('hit_rate'))} | {str(item.get('pattern_key') or '').replace('|', '/')} "
            f"| {gaps.replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Top chronological lessons",
            "",
            "| Time | Symbol | Horizon | Pred | Actual | Gap | Abs error | Memory action | Weak patterns |",
            "| --- | --- | --- | --- | --- | --- | ---: | --- | --- |",
        ]
    )
    lessons = result.get("top_lessons") or []
    if not lessons:
        lines.append("| - | - | - | - | - | - | - | - | - |")
    for item in lessons[:20]:
        weak = "; ".join(str(pattern.get("key") or "") for pattern in (item.get("weak_patterns") or [])[:2])
        lines.append(
            f"| {item.get('replay_timestamp')} | {item.get('symbol')} | {item.get('horizon')} "
            f"| {item.get('predicted_direction')} | {item.get('actual_direction')} | {item.get('gap_type')} "
            f"| {_fmt_signed_pct(item.get('absolute_excess_error'))} | {item.get('memory_action')} | {weak.replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is the first layer of the requested learning loop: it proves that the system can walk forward from a blank state, record prediction gaps, attach sub-source/regime/emergency context, and let only prior mistakes influence later memory gates. The next layer can attach optional OpenAI cause-analysis to the largest chronological lessons.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_learning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_learning_markdown(result, filters=filters), encoding="utf-8")
    return output_path
