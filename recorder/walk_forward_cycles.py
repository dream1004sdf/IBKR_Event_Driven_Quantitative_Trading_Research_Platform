from __future__ import annotations

import json
from bisect import bisect_left, bisect_right
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .economic_indicator_desk import build_economic_indicator_desk, compact_economic_indicator_desk
from .evidence_packet import build_overlay_evidence_packet
from .market_overlay import overlay_for_score
from .standing_regimes import active_standing_regimes_for_event
from .walk_forward_training import (
    _direction_hit,
    _gap,
    _metrics,
    _parse_dt,
    _pattern_keys,
    _prefix,
)


WALK_FORWARD_CYCLE_VERSION = "walk-forward-cycle-training-v1"
OVERLAY_SIGNAL_LOOKBACK_HOURS = 96.0


def _packet_cache_key(row: dict[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("id") or ""),
            str(row.get("outcome_id") or ""),
            str(row.get("replay_timestamp") or ""),
            str(row.get("symbol") or ""),
            str(row.get("horizon") or ""),
        )
    )


def _packet_context_cache_key(row: dict[str, Any]) -> str:
    """Stable evidence-context key independent of outcome/model horizon."""
    return "|".join(
        (
            str(row.get("replay_timestamp") or row.get("prediction_cutoff_at") or ""),
            str(row.get("symbol") or row.get("actual_subject_symbol") or "").upper(),
            str(row.get("predicted_direction") or "").lower(),
        )
    )


def _build_signal_window_index(signals: list[dict[str, Any]]) -> tuple[list[datetime], list[dict[str, Any]]]:
    parsed: list[tuple[datetime, dict[str, Any]]] = []
    min_dt = datetime.min.replace(tzinfo=UTC)
    for signal in signals:
        signal_time = _parse_dt(signal.get("published_at"))
        if signal_time <= min_dt:
            continue
        parsed.append((signal_time, signal))
    parsed.sort(key=lambda item: item[0])
    return [item[0] for item in parsed], [item[1] for item in parsed]


def _signals_for_event_window(
    row: dict[str, Any],
    signals: list[dict[str, Any]],
    signal_window_index: tuple[list[datetime], list[dict[str, Any]]] | None,
) -> list[dict[str, Any]]:
    if signal_window_index is None:
        return signals
    times, indexed_signals = signal_window_index
    if not times:
        return []
    event_time = _parse_dt(row.get("replay_timestamp") or row.get("prediction_cutoff_at"))
    if event_time <= datetime.min.replace(tzinfo=UTC):
        return signals
    start_time = event_time - timedelta(hours=OVERLAY_SIGNAL_LOOKBACK_HOURS)
    left = bisect_left(times, start_time)
    right = bisect_right(times, event_time)
    return indexed_signals[left:right]


def _compact_threshold_packet(packet: dict[str, Any]) -> dict[str, Any]:
    overlay = packet.get("overlay") or {}
    standing = packet.get("standing_regime_overlay") or {}
    economic = packet.get("economic_indicator_desk") or {}
    top_signals = [
        {"emergency": bool(item.get("emergency"))}
        for item in (overlay.get("top_signals") or [])
        if isinstance(item, dict) and bool(item.get("emergency"))
    ][:3]
    active_regimes = []
    for item in standing.get("active_regimes") or []:
        if not isinstance(item, dict):
            continue
        active_regimes.append(
            {
                "family": str(item.get("family") or ""),
                "directional_bias": str(item.get("directional_bias") or ""),
            }
        )
    return {
        "overlay": {
            "overlay_state": str(overlay.get("overlay_state") or "normal"),
            "overlay_action": str(overlay.get("overlay_action") or "none"),
            "matched_signal_count": int(overlay.get("matched_signal_count") or 0),
            "emergency": bool(overlay.get("emergency")),
            "urgency": str(overlay.get("urgency") or "unknown"),
            "top_signals": top_signals,
        },
        "standing_regime_overlay": {
            "matched_regime_count": int(standing.get("matched_regime_count") or len(active_regimes)),
            "active_regimes": active_regimes,
        },
        "economic_indicator_desk": {
            "risk_bias": str(economic.get("risk_bias") or ""),
        },
    }


def _build_lightweight_threshold_packet(
    row: dict[str, Any],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    economic_cache: dict[str, dict[str, Any]],
    standing_cache: dict[str, dict[str, Any]],
    signal_window_index: tuple[list[datetime], list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    event_time = _parse_dt(row.get("replay_timestamp") or row.get("prediction_cutoff_at"))
    if event_time is None:
        event_time = datetime.now(UTC)
    cutoff_date = event_time.date().isoformat()
    iso_year, iso_week, _ = event_time.isocalendar()
    macro_cache_key = f"{iso_year:04d}-W{iso_week:02d}"
    symbol = str(row.get("symbol") or row.get("actual_subject_symbol") or "").upper()
    predicted_direction = str(row.get("predicted_direction") or "")

    economic = economic_cache.get(macro_cache_key)
    if economic is None:
        economic = compact_economic_indicator_desk(
            build_economic_indicator_desk(db, cutoff_date=cutoff_date),
            max_indicators=0,
        )
        economic_cache[macro_cache_key] = economic

    # Standing regimes are intentionally long-lived context.  Cache them by
    # replay week instead of exact timestamp so long historical training jobs
    # do not issue one regime query per replay row.
    standing_key = "|".join((symbol, macro_cache_key, predicted_direction))
    standing = standing_cache.get(standing_key)
    if standing is None:
        try:
            standing = active_standing_regimes_for_event(
                db,
                symbol=symbol,
                cutoff_at=event_time,
                predicted_direction=predicted_direction,
                point_in_time=False,
            )
        except Exception as exc:  # pragma: no cover - defensive compatibility for old scratch DBs
            standing = {
                "standing_regime_version": "unavailable",
                "symbol": symbol,
                "cutoff_at": event_time.isoformat(),
                "bias": "neutral",
                "matched_regime_count": 0,
                "active_regimes": [],
                "error": str(exc)[:180],
            }
        standing_cache[standing_key] = standing

    return _compact_threshold_packet(
        {
            "overlay": overlay_for_score(row, _signals_for_event_window(row, external_signals, signal_window_index)),
            "standing_regime_overlay": standing,
            "economic_indicator_desk": economic,
        }
    )


def _stats_hit_rate(stats: dict[str, Any]) -> float | None:
    seen = int(stats.get("seen") or 0)
    if seen <= 0:
        return None
    return int(stats.get("hits") or 0) / seen


def _policy_hit_rate(entry: dict[str, Any]) -> float | None:
    if entry.get("hit_rate") is not None:
        return float(entry["hit_rate"])
    seen = int(entry.get("seen") or 0)
    if seen <= 0:
        return None
    return int(entry.get("hits") or 0) / seen


def _policy_action_for_key(entry: dict[str, Any]) -> str:
    action = str(entry.get("action") or entry.get("recommended_action") or "preserve")
    if action in {"reflex_observe_only", "observe_only_memory_gate"}:
        return action
    return "preserve"


def _policy_lookup(policy: dict[str, dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for key in keys:
        entry = policy.get(key)
        if not entry:
            continue
        action = _policy_action_for_key(entry)
        if action == "preserve":
            continue
        matches.append(
            {
                "key": key,
                "action": action,
                "seen": int(entry.get("seen") or 0),
                "hit_rate": _policy_hit_rate(entry),
                "source_cycle": entry.get("source_cycle"),
                "reason": entry.get("reason") or "",
            }
        )
    return matches


def _dynamic_weak_matches(
    memory: dict[str, dict[str, Any]],
    keys: list[str],
    *,
    min_pattern_observations: int,
    weak_hit_rate: float,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for key in keys:
        stats = memory.get(key)
        if not stats:
            continue
        seen = int(stats.get("seen") or 0)
        hit_rate = _stats_hit_rate(stats)
        if seen >= int(min_pattern_observations) and hit_rate is not None and hit_rate < float(weak_hit_rate):
            action = "reflex_observe_only" if _prefix(key) == "emergency_reflex" else "observe_only_memory_gate"
            matches.append({"key": key, "action": action, "seen": seen, "hit_rate": hit_rate, "source_cycle": "current"})
    return matches


def _row_with_gap(row: dict[str, Any]) -> dict[str, Any]:
    gap = _gap(row)
    return {
        **gap,
        "symbol": str(row.get("symbol") or "").upper(),
        "horizon": str(row.get("horizon") or ""),
        "replay_timestamp": str(row.get("replay_timestamp") or ""),
        "predicted_direction": str(row.get("predicted_direction") or ""),
        "actual_direction": str(row.get("actual_direction") or ""),
    }


def _simulate_cycle(
    ordered_rows: list[dict[str, Any]],
    packet_by_key: dict[str, dict[str, Any]],
    *,
    seed_policy: dict[str, dict[str, Any]],
    min_pattern_observations: int,
    weak_hit_rate: float,
    cycle: int,
) -> dict[str, Any]:
    memory: dict[str, dict[str, Any]] = {}
    simulated_rows: list[dict[str, Any]] = []
    action_counter: Counter[str] = Counter()
    trigger_counter: Counter[str] = Counter()

    for index, row in enumerate(ordered_rows, start=1):
        packet = packet_by_key[_packet_cache_key(row)]
        keys = _pattern_keys(row, packet)
        policy_matches = _policy_lookup(seed_policy, keys)
        dynamic_matches = _dynamic_weak_matches(
            memory,
            keys,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
        )
        matches = policy_matches + dynamic_matches
        if any(item["action"] == "reflex_observe_only" for item in matches):
            action = "reflex_observe_only"
        elif matches:
            action = "observe_only_memory_gate"
        else:
            action = "directional_signal"

        for item in matches[:5]:
            trigger_counter[str(item.get("key") or "")] += 1
        action_counter[action] += 1
        gap_row = _row_with_gap(row)
        simulated_rows.append(
            {
                **gap_row,
                "index": index,
                "cycle": cycle,
                "memory_action": action,
                "policy_matches": policy_matches[:5],
                "dynamic_matches": dynamic_matches[:5],
                "pattern_keys": keys,
            }
        )
        direction_hit = gap_row["direction_hit"]
        if direction_hit is not None:
            for key in keys:
                stats = memory.setdefault(key, {"seen": 0, "hits": 0, "gap_types": Counter(), "abs_error_sum": 0.0})
                stats["seen"] = int(stats.get("seen") or 0) + 1
                stats["hits"] = int(stats.get("hits") or 0) + int(direction_hit)
                if isinstance(stats.get("gap_types"), Counter):
                    stats["gap_types"][str(gap_row["gap_type"])] += 1
                if gap_row.get("absolute_excess_error") is not None:
                    stats["abs_error_sum"] = float(stats.get("abs_error_sum") or 0.0) + float(gap_row["absolute_excess_error"])

    tradeable = [row for row in simulated_rows if row["memory_action"] == "directional_signal"]
    observe_only = [row for row in simulated_rows if row["memory_action"] != "directional_signal"]
    all_metrics = _metrics(simulated_rows)
    tradeable_metrics = _metrics(tradeable)
    observe_metrics = _metrics(observe_only)
    return {
        "cycle": cycle,
        "all_rows": all_metrics,
        "tradeable_after_threshold": tradeable_metrics,
        "observe_only_after_threshold": observe_metrics,
        "coverage": (
            tradeable_metrics["directional_rows"] / all_metrics["directional_rows"]
            if all_metrics["directional_rows"]
            else None
        ),
        "action_counts": dict(action_counter),
        "top_triggered_patterns": [
            {"pattern_key": key, "rows": value}
            for key, value in trigger_counter.most_common(20)
            if key
        ],
        "memory": memory,
    }


def _promote_threshold_table(
    memory: dict[str, dict[str, Any]],
    *,
    cycle: int,
    min_pattern_observations: int,
    weak_hit_rate: float,
    max_entries: int,
) -> dict[str, dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key, stats in memory.items():
        seen = int(stats.get("seen") or 0)
        if seen < int(min_pattern_observations):
            continue
        hits = int(stats.get("hits") or 0)
        hit_rate = hits / seen if seen else None
        if hit_rate is None:
            continue
        track = _prefix(key)
        gap_types = dict(stats.get("gap_types") or {})
        action = "preserve"
        if hit_rate < float(weak_hit_rate):
            action = "reflex_observe_only" if track == "emergency_reflex" else "observe_only_memory_gate"
        abs_error_avg = float(stats.get("abs_error_sum") or 0.0) / seen if seen else None
        severity = (1.0 - hit_rate) * seen
        if action == "reflex_observe_only":
            severity += 10.0
        elif action == "observe_only_memory_gate":
            severity += 5.0
        entries.append(
            {
                "pattern_key": key,
                "track": track,
                "action": action,
                "seen": seen,
                "hits": hits,
                "hit_rate": hit_rate,
                "avg_absolute_excess_error": abs_error_avg,
                "weak_hit_rate_threshold": float(weak_hit_rate),
                "min_pattern_observations": int(min_pattern_observations),
                "gap_types": gap_types,
                "source_cycle": cycle,
                "reason": (
                    f"cycle {cycle} hit_rate {hit_rate:.2%} over {seen} observations; "
                    f"threshold {float(weak_hit_rate):.2%}"
                ),
                "severity": severity,
            }
        )
    entries.sort(key=lambda item: (item["action"] == "preserve", -float(item["severity"]), item["pattern_key"]))
    return {str(item["pattern_key"]): item for item in entries[: max(0, int(max_entries))]}


def _policy_summary(policy: dict[str, dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(_policy_action_for_key(entry) for entry in policy.values())
    tracks = Counter(str(entry.get("track") or _prefix(str(key))) for key, entry in policy.items())
    return {
        "entries": len(policy),
        "actions": dict(actions),
        "tracks": dict(tracks),
    }


def run_walk_forward_training_cycles(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    cycles: int = 3,
    min_pattern_observations: int = 8,
    weak_hit_rate: float = 0.45,
    max_threshold_entries: int = 120,
    packet_builder: Callable[[dict[str, Any], list[dict[str, Any]], Any], dict[str, Any]] = build_overlay_evidence_packet,
) -> dict[str, Any]:
    ordered_rows = sorted(rows, key=lambda row: (_parse_dt(row.get("replay_timestamp")), int(row.get("outcome_id") or 0)))
    packet_by_key = {
        _packet_cache_key(row): packet_builder(row, external_signals, db)
        for row in ordered_rows
    }
    policy: dict[str, dict[str, Any]] = {}
    cycle_results: list[dict[str, Any]] = []
    policy_snapshots: list[dict[str, Any]] = []

    for cycle in range(1, max(1, int(cycles)) + 1):
        result = _simulate_cycle(
            ordered_rows,
            packet_by_key,
            seed_policy=policy,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
            cycle=cycle,
        )
        next_policy = _promote_threshold_table(
            result["memory"],
            cycle=cycle,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
            max_entries=max_threshold_entries,
        )
        result_without_memory = {key: value for key, value in result.items() if key != "memory"}
        result_without_memory["input_policy_summary"] = _policy_summary(policy)
        result_without_memory["output_policy_summary"] = _policy_summary(next_policy)
        cycle_results.append(result_without_memory)
        policy_snapshots.append(
            {
                "cycle": cycle,
                "summary": _policy_summary(next_policy),
                "top_entries": list(next_policy.values())[:30],
            }
        )
        policy = next_policy

    baseline = cycle_results[0]["tradeable_after_threshold"] if cycle_results else {}
    final = cycle_results[-1]["tradeable_after_threshold"] if cycle_results else {}
    baseline_acc = baseline.get("directional_accuracy")
    final_acc = final.get("directional_accuracy")
    baseline_error = baseline.get("avg_absolute_excess_error")
    final_error = final.get("avg_absolute_excess_error")
    return {
        "version": WALK_FORWARD_CYCLE_VERSION,
        "rows": len(ordered_rows),
        "signals": len(external_signals),
        "data_time_range": {
            "start": str(ordered_rows[0].get("replay_timestamp") or "") if ordered_rows else None,
            "end": str(ordered_rows[-1].get("replay_timestamp") or "") if ordered_rows else None,
        },
        "cycles": len(cycle_results),
        "min_pattern_observations": int(min_pattern_observations),
        "weak_hit_rate": float(weak_hit_rate),
        "max_threshold_entries": int(max_threshold_entries),
        "cycle_results": cycle_results,
        "policy_snapshots": policy_snapshots,
        "final_threshold_table": list(policy.values()),
        "final_policy_summary": _policy_summary(policy),
        "improvement": {
            "tradeable_accuracy_delta": (
                float(final_acc) - float(baseline_acc)
                if final_acc is not None and baseline_acc is not None
                else None
            ),
            "tradeable_avg_abs_error_delta": (
                float(final_error) - float(baseline_error)
                if final_error is not None and baseline_error is not None
                else None
            ),
            "coverage_delta": (
                float(cycle_results[-1]["coverage"]) - float(cycle_results[0]["coverage"])
                if cycle_results and cycle_results[-1].get("coverage") is not None and cycle_results[0].get("coverage") is not None
                else None
            ),
        },
        "safety": {
            "cycle_policy": (
                "Cycle 1 starts from blank memory. After each full cycle, only the learned "
                "threshold table is carried into the next restart from the same chronological start."
            ),
            "mutation_policy": "Report-only; scorer rows, portfolio state, account state, and orders are not mutated.",
        },
    }


def _run_cycles_with_packet_cache(
    ordered_rows: list[dict[str, Any]],
    packet_by_key: dict[str, dict[str, Any]],
    *,
    cycles: int,
    min_pattern_observations: int,
    weak_hit_rate: float,
    max_threshold_entries: int,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    policy: dict[str, dict[str, Any]] = {}
    cycle_results: list[dict[str, Any]] = []
    for cycle in range(1, max(1, int(cycles)) + 1):
        result = _simulate_cycle(
            ordered_rows,
            packet_by_key,
            seed_policy=policy,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
            cycle=cycle,
        )
        next_policy = _promote_threshold_table(
            result["memory"],
            cycle=cycle,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
            max_entries=max_threshold_entries,
        )
        result_without_memory = {key: value for key, value in result.items() if key != "memory"}
        result_without_memory["input_policy_summary"] = _policy_summary(policy)
        result_without_memory["output_policy_summary"] = _policy_summary(next_policy)
        cycle_results.append(result_without_memory)
        if progress_callback is not None:
            progress_callback(
                {
                    "event": "cycle_completed",
                    "cycle": cycle,
                    "cycles": max(1, int(cycles)),
                    "min_pattern_observations": int(min_pattern_observations),
                    "weak_hit_rate": float(weak_hit_rate),
                    "max_threshold_entries": int(max_threshold_entries),
                    "coverage": result_without_memory.get("coverage"),
                    "tradeable_after_threshold": result_without_memory.get("tradeable_after_threshold"),
                    "output_policy_summary": result_without_memory.get("output_policy_summary"),
                }
            )
        policy = next_policy
    return {
        "cycle_results": cycle_results,
        "policy": policy,
    }


def _grid_values(text: str | None, cast: Callable[[str], Any]) -> list[Any]:
    values: list[Any] = []
    for item in str(text or "").split(","):
        item = item.strip()
        if not item:
            continue
        values.append(cast(item))
    return values


def _threshold_grid_key(min_obs: int, weak_rate: float, max_entries: int) -> str:
    return f"min_obs={int(min_obs)}|weak_rate={float(weak_rate):.8f}|max_entries={int(max_entries)}"


def _append_threshold_checkpoint(path: Path | None, payload: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "checkpoint_version": "walk-forward-threshold-checkpoint-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        **payload,
    }
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _load_threshold_checkpoint_candidates(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    candidates: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("event") != "candidate_completed":
                continue
            key = str(item.get("grid_key") or "")
            candidate = item.get("candidate")
            if key and isinstance(candidate, dict):
                candidates[key] = candidate
    return candidates


def _load_threshold_checkpoint_packets(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    packets: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if item.get("event") != "packet_cached":
                continue
            key = str(item.get("packet_key") or "")
            packet = item.get("packet")
            if key and isinstance(packet, dict):
                packets[key] = packet
    return packets


def tune_walk_forward_cycle_thresholds(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    cycles: int = 3,
    min_pattern_observations_grid: tuple[int, ...] = (8, 12, 16, 24),
    weak_hit_rate_grid: tuple[float, ...] = (0.38, 0.40, 0.42, 0.45),
    max_threshold_entries_grid: tuple[int, ...] = (20, 40, 80, 120),
    min_coverage: float = 0.45,
    coverage_weight: float = 0.15,
    abs_error_weight: float = 0.50,
    top_n: int = 20,
    packet_builder: Callable[[dict[str, Any], list[dict[str, Any]], Any], dict[str, Any]] = build_overlay_evidence_packet,
    checkpoint_jsonl_path: Path | None = None,
    resume_from_checkpoint: bool = True,
    packet_checkpoint_interval: int = 1000,
) -> dict[str, Any]:
    ordered_rows = sorted(rows, key=lambda row: (_parse_dt(row.get("replay_timestamp")), int(row.get("outcome_id") or 0)))
    checkpoint_path = Path(checkpoint_jsonl_path) if checkpoint_jsonl_path is not None else None
    total_grid = (
        len(tuple(min_pattern_observations_grid))
        * len(tuple(weak_hit_rate_grid))
        * len(tuple(max_threshold_entries_grid))
    )
    resumed_by_key = _load_threshold_checkpoint_candidates(checkpoint_path) if resume_from_checkpoint else {}
    packet_by_key: dict[str, dict[str, Any]] = (
        _load_threshold_checkpoint_packets(checkpoint_path) if resume_from_checkpoint else {}
    )
    use_lightweight_threshold_packets = packet_builder is build_overlay_evidence_packet
    signal_window_index = _build_signal_window_index(external_signals) if use_lightweight_threshold_packets else None
    economic_packet_cache: dict[str, dict[str, Any]] = {}
    standing_packet_cache: dict[str, dict[str, Any]] = {}
    context_packet_cache: dict[str, dict[str, Any]] = {}
    _append_threshold_checkpoint(
        checkpoint_path,
        {
            "event": "run_started",
            "rows": len(ordered_rows),
            "signals": len(external_signals),
            "total_grid_candidates": total_grid,
            "resumable_candidates_loaded": len(resumed_by_key),
            "resumable_packets_loaded": len(packet_by_key),
            "packet_builder_mode": (
                "lightweight_threshold_compact_v1"
                if use_lightweight_threshold_packets
                else "custom_packet_builder_compact_v1"
            ),
            "signal_window_lookback_hours": (
                OVERLAY_SIGNAL_LOOKBACK_HOURS if use_lightweight_threshold_packets else None
            ),
        },
    )
    packet_interval = max(1, int(packet_checkpoint_interval))
    for index, row in enumerate(ordered_rows, start=1):
        packet_key = _packet_cache_key(row)
        context_key = _packet_context_cache_key(row)
        if packet_key in packet_by_key:
            context_packet_cache.setdefault(context_key, packet_by_key[packet_key])
        else:
            packet = context_packet_cache.get(context_key)
            if packet is None:
                if use_lightweight_threshold_packets:
                    packet = _build_lightweight_threshold_packet(
                        row,
                        external_signals,
                        db,
                        economic_cache=economic_packet_cache,
                        standing_cache=standing_packet_cache,
                        signal_window_index=signal_window_index,
                    )
                else:
                    packet = _compact_threshold_packet(packet_builder(row, external_signals, db))
                context_packet_cache[context_key] = packet
            packet_by_key[packet_key] = packet
            _append_threshold_checkpoint(
                checkpoint_path,
                {
                    "event": "packet_cached",
                    "packet_key": packet_key,
                    "context_key": context_key,
                    "row_index": index,
                    "packet_format": "threshold_compact_v1",
                    "packet": packet_by_key[packet_key],
                },
            )
        if index == 1 or index == len(ordered_rows) or index % packet_interval == 0:
            _append_threshold_checkpoint(
                checkpoint_path,
                {
                    "event": "packet_cache_progress",
                    "processed_rows": index,
                    "total_rows": len(ordered_rows),
                    "progress": (index / len(ordered_rows)) if ordered_rows else 1.0,
                    "cached_packets": len(packet_by_key),
                    "cached_context_packets": len(context_packet_cache),
                },
            )
    candidates: list[dict[str, Any]] = []
    resumed_candidates = 0
    evaluated_grid_candidates = 0
    grid_index = 0
    for min_obs in min_pattern_observations_grid:
        for weak_rate in weak_hit_rate_grid:
            for max_entries in max_threshold_entries_grid:
                grid_index += 1
                grid_key = _threshold_grid_key(int(min_obs), float(weak_rate), int(max_entries))
                if grid_key in resumed_by_key:
                    candidates.append(resumed_by_key[grid_key])
                    resumed_candidates += 1
                    _append_threshold_checkpoint(
                        checkpoint_path,
                        {
                            "event": "candidate_resumed",
                            "grid_key": grid_key,
                            "grid_index": grid_index,
                            "total_grid_candidates": total_grid,
                        },
                    )
                    continue
                _append_threshold_checkpoint(
                    checkpoint_path,
                    {
                        "event": "candidate_started",
                        "grid_key": grid_key,
                        "grid_index": grid_index,
                        "total_grid_candidates": total_grid,
                        "min_pattern_observations": int(min_obs),
                        "weak_hit_rate": float(weak_rate),
                        "max_threshold_entries": int(max_entries),
                    },
                )

                def _progress(payload: dict[str, Any]) -> None:
                    _append_threshold_checkpoint(
                        checkpoint_path,
                        {
                            **payload,
                            "grid_key": grid_key,
                            "grid_index": grid_index,
                            "total_grid_candidates": total_grid,
                        },
                    )

                run = _run_cycles_with_packet_cache(
                    ordered_rows,
                    packet_by_key,
                    cycles=cycles,
                    min_pattern_observations=int(min_obs),
                    weak_hit_rate=float(weak_rate),
                    max_threshold_entries=int(max_entries),
                    progress_callback=_progress,
                )
                evaluated_grid_candidates += 1
                cycle_results = run["cycle_results"]
                if not cycle_results:
                    continue
                first = cycle_results[0]
                final = cycle_results[-1]
                tradeable = final.get("tradeable_after_threshold") or {}
                first_tradeable = first.get("tradeable_after_threshold") or {}
                accuracy = tradeable.get("directional_accuracy")
                coverage = final.get("coverage")
                avg_abs_error = tradeable.get("avg_absolute_excess_error")
                if accuracy is None or coverage is None:
                    continue
                passes_coverage = float(coverage) >= float(min_coverage)
                score = float(accuracy) + float(coverage_weight) * float(coverage)
                if avg_abs_error is not None:
                    score -= float(abs_error_weight) * float(avg_abs_error)
                candidate = {
                    "min_pattern_observations": int(min_obs),
                    "weak_hit_rate": float(weak_rate),
                    "max_threshold_entries": int(max_entries),
                    "cycles": len(cycle_results),
                    "passes_min_coverage": passes_coverage,
                    "score": score,
                    "tradeable_accuracy": accuracy,
                    "tradeable_directional_hits": tradeable.get("directional_hits"),
                    "tradeable_directional_rows": tradeable.get("directional_rows"),
                    "coverage": coverage,
                    "avg_absolute_excess_error": avg_abs_error,
                    "accuracy_delta_vs_cycle1": (
                        float(accuracy) - float(first_tradeable.get("directional_accuracy"))
                        if first_tradeable.get("directional_accuracy") is not None
                        else None
                    ),
                    "coverage_delta_vs_cycle1": (
                        float(coverage) - float(first.get("coverage"))
                        if first.get("coverage") is not None
                        else None
                    ),
                    "final_policy_summary": _policy_summary(run["policy"]),
                    "final_threshold_table": list(run["policy"].values())[:30],
                    "cycle_results": cycle_results,
                }
                candidates.append(candidate)
                _append_threshold_checkpoint(
                    checkpoint_path,
                    {
                        "event": "candidate_completed",
                        "grid_key": grid_key,
                        "grid_index": grid_index,
                        "total_grid_candidates": total_grid,
                        "candidate": candidate,
                    },
                )
    candidates.sort(
        key=lambda item: (
            not bool(item["passes_min_coverage"]),
            -float(item["score"]),
            -float(item["tradeable_accuracy"]),
            -float(item["coverage"]),
        )
    )
    selected = candidates[0] if candidates else None
    return {
        "version": WALK_FORWARD_CYCLE_VERSION,
        "tuner": "walk-forward-threshold-pareto-tune-v1",
        "rows": len(ordered_rows),
        "signals": len(external_signals),
        "data_time_range": {
            "start": str(ordered_rows[0].get("replay_timestamp") or "") if ordered_rows else None,
            "end": str(ordered_rows[-1].get("replay_timestamp") or "") if ordered_rows else None,
        },
        "grid": {
            "cycles": int(cycles),
            "min_pattern_observations_grid": list(min_pattern_observations_grid),
            "weak_hit_rate_grid": list(weak_hit_rate_grid),
            "max_threshold_entries_grid": list(max_threshold_entries_grid),
            "min_coverage": float(min_coverage),
            "coverage_weight": float(coverage_weight),
            "abs_error_weight": float(abs_error_weight),
        },
        "candidates_evaluated": len(candidates),
        "checkpoint": {
            "checkpoint_jsonl_path": str(checkpoint_path) if checkpoint_path else None,
            "resumed_candidates": resumed_candidates,
            "newly_evaluated_candidates": evaluated_grid_candidates,
            "total_grid_candidates": total_grid,
            "packet_checkpoint_interval": packet_interval,
            "cached_packets": len(packet_by_key),
            "cached_context_packets": len(context_packet_cache),
            "packet_builder_mode": (
                "lightweight_threshold_compact_v1"
                if use_lightweight_threshold_packets
                else "custom_packet_builder_compact_v1"
            ),
            "signal_window_lookback_hours": (
                OVERLAY_SIGNAL_LOOKBACK_HOURS if use_lightweight_threshold_packets else None
            ),
            "resume_from_checkpoint": bool(resume_from_checkpoint),
        },
        "selected": selected,
        "top_candidates": candidates[: max(0, int(top_n))],
        "safety": {
            "cycle_policy": "Each grid candidate restarts from the same chronological start and carries only its learned threshold table between cycles.",
            "mutation_policy": "Report-only; scorer rows, portfolio state, account state, and orders are not mutated.",
        },
    }


def _nuanced_action_priority(action: str) -> int:
    return {
        "reflex_observe_only": 5,
        "second_pass_review": 4,
        "neutral_candidate": 3,
        "confidence_haircut": 2,
        "preserve": 1,
    }.get(str(action or "preserve"), 1)


def _nuanced_policy_action(entry: dict[str, Any]) -> str:
    action = str(entry.get("action") or "preserve")
    if action in {"reflex_observe_only", "second_pass_review", "neutral_candidate", "confidence_haircut"}:
        return action
    return "preserve"


def _nuanced_policy_lookup(policy: dict[str, dict[str, Any]], keys: list[str]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for key in keys:
        entry = policy.get(key)
        if not entry:
            continue
        action = _nuanced_policy_action(entry)
        if action == "preserve":
            continue
        matches.append(
            {
                "key": key,
                "action": action,
                "seen": int(entry.get("seen") or 0),
                "hit_rate": _policy_hit_rate(entry),
                "flat_rate": entry.get("flat_rate"),
                "source_cycle": entry.get("source_cycle"),
                "reason": entry.get("reason") or "",
            }
        )
    return matches


def _select_nuanced_action(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return "directional_signal"
    return max((str(item.get("action") or "preserve") for item in matches), key=_nuanced_action_priority)


def _is_tradeable_nuanced(action: str) -> bool:
    return action in {"directional_signal", "confidence_haircut"}


def _nuanced_memory_stats_template() -> dict[str, Any]:
    return {
        "seen": 0,
        "hits": 0,
        "gap_types": Counter(),
        "actual_directions": Counter(),
        "abs_error_sum": 0.0,
    }


def _promote_nuanced_threshold_table(
    memory: dict[str, dict[str, Any]],
    *,
    cycle: int,
    min_pattern_observations: int,
    reflex_hit_rate: float,
    second_pass_hit_rate: float,
    haircut_hit_rate: float,
    neutral_flat_rate: float,
    max_entries: int,
) -> dict[str, dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for key, stats in memory.items():
        seen = int(stats.get("seen") or 0)
        if seen < int(min_pattern_observations):
            continue
        hits = int(stats.get("hits") or 0)
        hit_rate = hits / seen if seen else None
        if hit_rate is None:
            continue
        track = _prefix(key)
        actual_directions = dict(stats.get("actual_directions") or {})
        flat_rate = int(actual_directions.get("flat") or 0) / seen if seen else 0.0
        action = "preserve"
        if track == "emergency_reflex" and hit_rate < float(reflex_hit_rate):
            action = "reflex_observe_only"
        elif flat_rate >= float(neutral_flat_rate) and hit_rate < float(haircut_hit_rate):
            action = "neutral_candidate"
        elif hit_rate < float(second_pass_hit_rate):
            action = "second_pass_review"
        elif hit_rate < float(haircut_hit_rate):
            action = "confidence_haircut"
        abs_error_avg = float(stats.get("abs_error_sum") or 0.0) / seen if seen else None
        severity = (1.0 - hit_rate) * seen + _nuanced_action_priority(action) * 3.0
        entries.append(
            {
                "pattern_key": key,
                "track": track,
                "action": action,
                "seen": seen,
                "hits": hits,
                "hit_rate": hit_rate,
                "flat_rate": flat_rate,
                "avg_absolute_excess_error": abs_error_avg,
                "min_pattern_observations": int(min_pattern_observations),
                "reflex_hit_rate": float(reflex_hit_rate),
                "second_pass_hit_rate": float(second_pass_hit_rate),
                "haircut_hit_rate": float(haircut_hit_rate),
                "neutral_flat_rate": float(neutral_flat_rate),
                "gap_types": dict(stats.get("gap_types") or {}),
                "actual_directions": actual_directions,
                "source_cycle": cycle,
                "reason": (
                    f"cycle {cycle} hit_rate {hit_rate:.2%}, flat_rate {flat_rate:.2%}, "
                    f"seen {seen}; action {action}"
                ),
                "severity": severity,
            }
        )
    entries.sort(key=lambda item: (item["action"] == "preserve", -float(item["severity"]), item["pattern_key"]))
    return {str(item["pattern_key"]): item for item in entries[: max(0, int(max_entries))]}


def _nuanced_policy_summary(policy: dict[str, dict[str, Any]]) -> dict[str, Any]:
    actions = Counter(_nuanced_policy_action(entry) for entry in policy.values())
    tracks = Counter(str(entry.get("track") or _prefix(str(key))) for key, entry in policy.items())
    return {"entries": len(policy), "actions": dict(actions), "tracks": dict(tracks)}


def _simulate_nuanced_cycle(
    ordered_rows: list[dict[str, Any]],
    packet_by_key: dict[str, dict[str, Any]],
    *,
    seed_policy: dict[str, dict[str, Any]],
    cycle: int,
) -> dict[str, Any]:
    memory: dict[str, dict[str, Any]] = {}
    simulated_rows: list[dict[str, Any]] = []
    action_counter: Counter[str] = Counter()
    trigger_counter: Counter[str] = Counter()

    for index, row in enumerate(ordered_rows, start=1):
        packet = packet_by_key[_packet_cache_key(row)]
        keys = _pattern_keys(row, packet)
        policy_matches = _nuanced_policy_lookup(seed_policy, keys)
        action = _select_nuanced_action(policy_matches)
        for item in policy_matches[:5]:
            trigger_counter[str(item.get("key") or "")] += 1
        action_counter[action] += 1
        gap_row = _row_with_gap(row)
        adjusted_direction = "flat" if action == "neutral_candidate" else gap_row["predicted_direction"]
        adjusted_hit = None
        actual_direction = str(gap_row.get("actual_direction") or "").lower()
        if adjusted_direction in {"up", "down", "flat"} and actual_direction in {"up", "down", "flat"}:
            adjusted_hit = int(adjusted_direction == actual_direction)
        simulated_rows.append(
            {
                **gap_row,
                "index": index,
                "cycle": cycle,
                "memory_action": action,
                "adjusted_direction": adjusted_direction,
                "adjusted_direction_hit": adjusted_hit,
                "policy_matches": policy_matches[:5],
                "pattern_keys": keys,
            }
        )
        direction_hit = gap_row["direction_hit"]
        if direction_hit is not None:
            for key in keys:
                stats = memory.setdefault(key, _nuanced_memory_stats_template())
                stats["seen"] = int(stats.get("seen") or 0) + 1
                stats["hits"] = int(stats.get("hits") or 0) + int(direction_hit)
                if isinstance(stats.get("gap_types"), Counter):
                    stats["gap_types"][str(gap_row["gap_type"])] += 1
                if isinstance(stats.get("actual_directions"), Counter):
                    stats["actual_directions"][actual_direction or "unknown"] += 1
                if gap_row.get("absolute_excess_error") is not None:
                    stats["abs_error_sum"] = float(stats.get("abs_error_sum") or 0.0) + float(gap_row["absolute_excess_error"])

    tradeable = [row for row in simulated_rows if _is_tradeable_nuanced(str(row["memory_action"]))]
    conservative = [row for row in simulated_rows if not _is_tradeable_nuanced(str(row["memory_action"]))]
    haircut = [row for row in simulated_rows if row["memory_action"] == "confidence_haircut"]
    clean = [row for row in simulated_rows if row["memory_action"] == "directional_signal"]
    all_metrics = _metrics(simulated_rows)
    tradeable_metrics = _metrics(tradeable)
    conservative_metrics = _metrics(conservative)
    return {
        "cycle": cycle,
        "all_rows": all_metrics,
        "tradeable_after_nuanced_action": tradeable_metrics,
        "clean_directional": _metrics(clean),
        "confidence_haircut": _metrics(haircut),
        "conservative_after_nuanced_action": conservative_metrics,
        "coverage": (
            tradeable_metrics["directional_rows"] / all_metrics["directional_rows"]
            if all_metrics["directional_rows"]
            else None
        ),
        "action_counts": dict(action_counter),
        "top_triggered_patterns": [
            {"pattern_key": key, "rows": value}
            for key, value in trigger_counter.most_common(20)
            if key
        ],
        "memory": memory,
    }


def run_walk_forward_nuanced_action_cycles(
    rows: list[dict[str, Any]],
    external_signals: list[dict[str, Any]],
    db: Any,
    *,
    cycles: int = 4,
    min_pattern_observations: int = 12,
    reflex_hit_rate: float = 0.30,
    second_pass_hit_rate: float = 0.36,
    haircut_hit_rate: float = 0.46,
    neutral_flat_rate: float = 0.45,
    max_threshold_entries: int = 20,
    packet_builder: Callable[[dict[str, Any], list[dict[str, Any]], Any], dict[str, Any]] = build_overlay_evidence_packet,
) -> dict[str, Any]:
    ordered_rows = sorted(rows, key=lambda row: (_parse_dt(row.get("replay_timestamp")), int(row.get("outcome_id") or 0)))
    use_lightweight_threshold_packets = packet_builder is build_overlay_evidence_packet
    signal_window_index = _build_signal_window_index(external_signals) if use_lightweight_threshold_packets else None
    economic_packet_cache: dict[str, dict[str, Any]] = {}
    standing_packet_cache: dict[str, dict[str, Any]] = {}
    context_packet_cache: dict[str, dict[str, Any]] = {}
    packet_by_key: dict[str, dict[str, Any]] = {}
    for row in ordered_rows:
        packet_key = _packet_cache_key(row)
        if packet_key in packet_by_key:
            continue
        context_key = _packet_context_cache_key(row)
        packet = context_packet_cache.get(context_key)
        if packet is None:
            if use_lightweight_threshold_packets:
                packet = _build_lightweight_threshold_packet(
                    row,
                    external_signals,
                    db,
                    economic_cache=economic_packet_cache,
                    standing_cache=standing_packet_cache,
                    signal_window_index=signal_window_index,
                )
            else:
                packet = _compact_threshold_packet(packet_builder(row, external_signals, db))
            context_packet_cache[context_key] = packet
        packet_by_key[packet_key] = packet
    policy: dict[str, dict[str, Any]] = {}
    cycle_results: list[dict[str, Any]] = []
    policy_snapshots: list[dict[str, Any]] = []
    for cycle in range(1, max(1, int(cycles)) + 1):
        result = _simulate_nuanced_cycle(ordered_rows, packet_by_key, seed_policy=policy, cycle=cycle)
        next_policy = _promote_nuanced_threshold_table(
            result["memory"],
            cycle=cycle,
            min_pattern_observations=min_pattern_observations,
            reflex_hit_rate=reflex_hit_rate,
            second_pass_hit_rate=second_pass_hit_rate,
            haircut_hit_rate=haircut_hit_rate,
            neutral_flat_rate=neutral_flat_rate,
            max_entries=max_threshold_entries,
        )
        result_without_memory = {key: value for key, value in result.items() if key != "memory"}
        result_without_memory["input_policy_summary"] = _nuanced_policy_summary(policy)
        result_without_memory["output_policy_summary"] = _nuanced_policy_summary(next_policy)
        cycle_results.append(result_without_memory)
        policy_snapshots.append({"cycle": cycle, "summary": _nuanced_policy_summary(next_policy), "top_entries": list(next_policy.values())[:30]})
        policy = next_policy
    baseline = cycle_results[0]["tradeable_after_nuanced_action"] if cycle_results else {}
    final = cycle_results[-1]["tradeable_after_nuanced_action"] if cycle_results else {}
    baseline_acc = baseline.get("directional_accuracy")
    final_acc = final.get("directional_accuracy")
    return {
        "version": WALK_FORWARD_CYCLE_VERSION,
        "variant": "walk-forward-nuanced-action-training-v1",
        "rows": len(ordered_rows),
        "signals": len(external_signals),
        "data_time_range": {
            "start": str(ordered_rows[0].get("replay_timestamp") or "") if ordered_rows else None,
            "end": str(ordered_rows[-1].get("replay_timestamp") or "") if ordered_rows else None,
        },
        "cycles": len(cycle_results),
        "packet_builder_mode": (
            "lightweight_threshold_compact_v1"
            if use_lightweight_threshold_packets
            else "custom_packet_builder_compact_v1"
        ),
        "signal_window_lookback_hours": (
            OVERLAY_SIGNAL_LOOKBACK_HOURS if use_lightweight_threshold_packets else None
        ),
        "cached_packets": len(packet_by_key),
        "cached_context_packets": len(context_packet_cache),
        "min_pattern_observations": int(min_pattern_observations),
        "reflex_hit_rate": float(reflex_hit_rate),
        "second_pass_hit_rate": float(second_pass_hit_rate),
        "haircut_hit_rate": float(haircut_hit_rate),
        "neutral_flat_rate": float(neutral_flat_rate),
        "max_threshold_entries": int(max_threshold_entries),
        "cycle_results": cycle_results,
        "policy_snapshots": policy_snapshots,
        "final_threshold_table": list(policy.values()),
        "final_policy_summary": _nuanced_policy_summary(policy),
        "improvement": {
            "tradeable_accuracy_delta": (
                float(final_acc) - float(baseline_acc)
                if final_acc is not None and baseline_acc is not None
                else None
            ),
            "coverage_delta": (
                float(cycle_results[-1]["coverage"]) - float(cycle_results[0]["coverage"])
                if cycle_results and cycle_results[-1].get("coverage") is not None and cycle_results[0].get("coverage") is not None
                else None
            ),
        },
        "safety": {
            "cycle_policy": "Cycle 1 starts blank; later cycles carry only the learned nuanced action table.",
            "mutation_policy": "Report-only; scorer rows, portfolio state, account state, and orders are not mutated.",
        },
    }


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2%}"


def _fmt_delta_pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):+.2%}"


def walk_forward_training_cycles_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Training Cycle Report",
) -> str:
    improvement = result.get("improvement") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{result.get('version')}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Data timestamp range: `{(result.get('data_time_range') or {}).get('start') or '-'} -> {(result.get('data_time_range') or {}).get('end') or '-'}`",
        f"- External signals available: `{result.get('signals')}`",
        f"- Cycles: `{result.get('cycles')}`",
        f"- Packet builder mode: `{result.get('packet_builder_mode') or '-'}`",
        f"- Signal window lookback hours: `{result.get('signal_window_lookback_hours') if result.get('signal_window_lookback_hours') is not None else '-'}`",
        f"- Cached packets: `{result.get('cached_packets') if result.get('cached_packets') is not None else '-'}`",
        f"- Cached context packets: `{result.get('cached_context_packets') if result.get('cached_context_packets') is not None else '-'}`",
        f"- Min pattern observations: `{result.get('min_pattern_observations')}`",
        f"- Weak-hit-rate threshold: `{_fmt_pct(result.get('weak_hit_rate'))}`",
        f"- Final threshold entries: `{(result.get('final_policy_summary') or {}).get('entries')}`",
        f"- Tradeable accuracy delta from cycle 1 to final: `{_fmt_delta_pct(improvement.get('tradeable_accuracy_delta'))}`",
        f"- Tradeable avg absolute-error delta from cycle 1 to final: `{_fmt_delta_pct(improvement.get('tradeable_avg_abs_error_delta'))}`",
        f"- Coverage delta from cycle 1 to final: `{_fmt_delta_pct(improvement.get('coverage_delta'))}`",
        "- Cycle policy: cycle 1 starts blank; after each full cycle, only the learned threshold table is carried into the next restart.",
        "- Safety: report-only; no scorer mutation, IBKR account query, portfolio access, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Cycle leaderboard",
            "",
            "| Cycle | Input table | Output table | All accuracy | Tradeable accuracy | Observe-only accuracy | Coverage | Avg abs error tradeable | Actions |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in result.get("cycle_results") or []:
        all_rows = item.get("all_rows") or {}
        tradeable = item.get("tradeable_after_threshold") or {}
        observe = item.get("observe_only_after_threshold") or {}
        actions = ", ".join(f"{key}:{value}" for key, value in sorted((item.get("action_counts") or {}).items()))
        lines.append(
            f"| {item.get('cycle')} | {(item.get('input_policy_summary') or {}).get('entries')} "
            f"| {(item.get('output_policy_summary') or {}).get('entries')} "
            f"| {all_rows.get('directional_hits')}/{all_rows.get('directional_rows')} {_fmt_pct(all_rows.get('directional_accuracy'))} "
            f"| {tradeable.get('directional_hits')}/{tradeable.get('directional_rows')} {_fmt_pct(tradeable.get('directional_accuracy'))} "
            f"| {observe.get('directional_hits')}/{observe.get('directional_rows')} {_fmt_pct(observe.get('directional_accuracy'))} "
            f"| {_fmt_pct(item.get('coverage'))} "
            f"| {_fmt_pct(tradeable.get('avg_absolute_excess_error'))} "
            f"| {actions.replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Final threshold table sample",
            "",
            "| Action | Track | Seen | Hit rate | Avg abs error | Pattern | Reason |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    entries = result.get("final_threshold_table") or []
    if not entries:
        lines.append("| - | - | 0 | - | - | - | - |")
    for entry in entries[:40]:
        lines.append(
            f"| {entry.get('action')} | {entry.get('track')} | {entry.get('seen')} "
            f"| {_fmt_pct(entry.get('hit_rate'))} | {_fmt_pct(entry.get('avg_absolute_excess_error'))} "
            f"| {str(entry.get('pattern_key') or '').replace('|', '/')} "
            f"| {str(entry.get('reason') or '').replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This is an iterative training loop, not a live trading rule. It deliberately restarts from the original chronological start after each cycle and carries forward only the learned threshold table. The goal is to reduce actionable prediction gaps by filtering or reflex-gating historically weak patterns before a later scorer version changes prediction direction or probability.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_training_cycles_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_training_cycles_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def write_walk_forward_training_cycles_json(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return output_path


def write_threshold_table_json(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": result.get("version"),
        "generated_at": datetime.now(UTC).isoformat(),
        "cycles": result.get("cycles"),
        "min_pattern_observations": result.get("min_pattern_observations"),
        "weak_hit_rate": result.get("weak_hit_rate"),
        "summary": result.get("final_policy_summary"),
        "threshold_table": result.get("final_threshold_table") or [],
        "safety": result.get("safety") or {},
    }
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return output_path


def walk_forward_threshold_tuning_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Threshold Pareto Tuning Report",
) -> str:
    selected = result.get("selected") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{result.get('version')}`",
        f"- Tuner: `{result.get('tuner')}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Data timestamp range: `{(result.get('data_time_range') or {}).get('start') or '-'} -> {(result.get('data_time_range') or {}).get('end') or '-'}`",
        f"- External signals available: `{result.get('signals')}`",
        f"- Candidates evaluated: `{result.get('candidates_evaluated')}`",
        f"- Selected min observations: `{selected.get('min_pattern_observations') if selected else '-'}`",
        f"- Selected weak-hit-rate: `{_fmt_pct(selected.get('weak_hit_rate') if selected else None)}`",
        f"- Selected max threshold entries: `{selected.get('max_threshold_entries') if selected else '-'}`",
        f"- Selected tradeable accuracy: `{selected.get('tradeable_directional_hits') if selected else '-'}/{selected.get('tradeable_directional_rows') if selected else '-'} {_fmt_pct(selected.get('tradeable_accuracy') if selected else None)}`",
        f"- Selected coverage: `{_fmt_pct(selected.get('coverage') if selected else None)}`",
        f"- Selected score: `{float(selected.get('score')):.4f}`" if selected.get("score") is not None else "- Selected score: `-`",
        "- Safety: report-only; no scorer mutation, IBKR account query, portfolio access, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    grid = result.get("grid") or {}
    lines.extend(["", "## Grid", ""])
    for key, value in grid.items():
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(
        [
            "",
            "## Top candidates",
            "",
            "| Rank | Pass coverage | Score | Min obs | Weak hit | Max entries | Accuracy | Coverage | Avg abs error | Acc delta | Coverage delta | Policy actions |",
            "| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    candidates = result.get("top_candidates") or []
    if not candidates:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | - |")
    for index, item in enumerate(candidates, start=1):
        actions = ", ".join(
            f"{key}:{value}"
            for key, value in sorted(((item.get("final_policy_summary") or {}).get("actions") or {}).items())
        )
        lines.append(
            f"| {index} | {item.get('passes_min_coverage')} | {float(item.get('score') or 0.0):.4f} "
            f"| {item.get('min_pattern_observations')} | {_fmt_pct(item.get('weak_hit_rate'))} "
            f"| {item.get('max_threshold_entries')} "
            f"| {item.get('tradeable_directional_hits')}/{item.get('tradeable_directional_rows')} {_fmt_pct(item.get('tradeable_accuracy'))} "
            f"| {_fmt_pct(item.get('coverage'))} | {_fmt_pct(item.get('avg_absolute_excess_error'))} "
            f"| {_fmt_delta_pct(item.get('accuracy_delta_vs_cycle1'))} | {_fmt_delta_pct(item.get('coverage_delta_vs_cycle1'))} "
            f"| {actions.replace('|', '/')} |"
        )
    if selected:
        lines.extend(
            [
                "",
                "## Selected threshold table sample",
                "",
                "| Action | Track | Seen | Hit rate | Pattern |",
                "| --- | --- | ---: | ---: | --- |",
            ]
        )
        for entry in selected.get("final_threshold_table") or []:
            lines.append(
                f"| {entry.get('action')} | {entry.get('track')} | {entry.get('seen')} "
                f"| {_fmt_pct(entry.get('hit_rate'))} | {str(entry.get('pattern_key') or '').replace('|', '/')} |"
            )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "This tuner searches for a less brittle threshold table by optimizing a simple Pareto score over tradeable accuracy, coverage, and absolute-error penalty. The selected candidate is still a report-only threshold policy; promotion into a live scorer or postscore gate should remain a separate reviewed step.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_threshold_tuning_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_threshold_tuning_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def write_walk_forward_threshold_tuning_json(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return output_path


def walk_forward_nuanced_actions_markdown(
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
    title: str = "Walk-Forward Nuanced Action Training Report",
) -> str:
    improvement = result.get("improvement") or {}
    lines = [
        f"# {title}",
        "",
        f"- Generated at: `{datetime.now(UTC).isoformat()}`",
        f"- Version: `{result.get('version')}`",
        f"- Variant: `{result.get('variant')}`",
        f"- Rows: `{result.get('rows')}`",
        f"- Data timestamp range: `{(result.get('data_time_range') or {}).get('start') or '-'} -> {(result.get('data_time_range') or {}).get('end') or '-'}`",
        f"- External signals available: `{result.get('signals')}`",
        f"- Cycles: `{result.get('cycles')}`",
        f"- Min pattern observations: `{result.get('min_pattern_observations')}`",
        f"- Reflex hit-rate threshold: `{_fmt_pct(result.get('reflex_hit_rate'))}`",
        f"- Second-pass hit-rate threshold: `{_fmt_pct(result.get('second_pass_hit_rate'))}`",
        f"- Confidence-haircut hit-rate threshold: `{_fmt_pct(result.get('haircut_hit_rate'))}`",
        f"- Neutral flat-rate threshold: `{_fmt_pct(result.get('neutral_flat_rate'))}`",
        f"- Final threshold entries: `{(result.get('final_policy_summary') or {}).get('entries')}`",
        f"- Tradeable accuracy delta from cycle 1 to final: `{_fmt_delta_pct(improvement.get('tradeable_accuracy_delta'))}`",
        f"- Coverage delta from cycle 1 to final: `{_fmt_delta_pct(improvement.get('coverage_delta'))}`",
        "- Action policy: `confidence_haircut` remains tradeable; `second_pass_review`, `neutral_candidate`, and `reflex_observe_only` are conservative actions.",
        "- Safety: report-only; no scorer mutation, IBKR account query, portfolio access, or order action.",
        "",
        "## Filters",
        "",
    ]
    if filters:
        for key, value in filters.items():
            lines.append(f"- `{key}`: `{value if value is not None else '-'}`")
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Cycle leaderboard",
            "",
            "| Cycle | Input table | Output table | All accuracy | Tradeable accuracy | Clean accuracy | Haircut accuracy | Conservative accuracy | Coverage | Actions |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for item in result.get("cycle_results") or []:
        all_rows = item.get("all_rows") or {}
        tradeable = item.get("tradeable_after_nuanced_action") or {}
        clean = item.get("clean_directional") or {}
        haircut = item.get("confidence_haircut") or {}
        conservative = item.get("conservative_after_nuanced_action") or {}
        actions = ", ".join(f"{key}:{value}" for key, value in sorted((item.get("action_counts") or {}).items()))
        lines.append(
            f"| {item.get('cycle')} | {(item.get('input_policy_summary') or {}).get('entries')} "
            f"| {(item.get('output_policy_summary') or {}).get('entries')} "
            f"| {all_rows.get('directional_hits')}/{all_rows.get('directional_rows')} {_fmt_pct(all_rows.get('directional_accuracy'))} "
            f"| {tradeable.get('directional_hits')}/{tradeable.get('directional_rows')} {_fmt_pct(tradeable.get('directional_accuracy'))} "
            f"| {clean.get('directional_hits')}/{clean.get('directional_rows')} {_fmt_pct(clean.get('directional_accuracy'))} "
            f"| {haircut.get('directional_hits')}/{haircut.get('directional_rows')} {_fmt_pct(haircut.get('directional_accuracy'))} "
            f"| {conservative.get('directional_hits')}/{conservative.get('directional_rows')} {_fmt_pct(conservative.get('directional_accuracy'))} "
            f"| {_fmt_pct(item.get('coverage'))} | {actions.replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Final nuanced action table sample",
            "",
            "| Action | Track | Seen | Hit rate | Flat rate | Pattern | Reason |",
            "| --- | --- | ---: | ---: | ---: | --- | --- |",
        ]
    )
    entries = result.get("final_threshold_table") or []
    if not entries:
        lines.append("| - | - | 0 | - | - | - | - |")
    for entry in entries[:50]:
        lines.append(
            f"| {entry.get('action')} | {entry.get('track')} | {entry.get('seen')} "
            f"| {_fmt_pct(entry.get('hit_rate'))} | {_fmt_pct(entry.get('flat_rate'))} "
            f"| {str(entry.get('pattern_key') or '').replace('|', '/')} "
            f"| {str(entry.get('reason') or '').replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Interpretation note",
            "",
            "M87 does not change stored scorer directions. It creates a more expressive action layer so that weak patterns can receive a confidence haircut, a second-pass review flag, a neutral-candidate flag, or an emergency reflex gate instead of being collapsed into one observe-only bucket.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_walk_forward_nuanced_actions_markdown(
    output_path: Path,
    result: dict[str, Any],
    *,
    filters: dict[str, Any] | None = None,
) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(walk_forward_nuanced_actions_markdown(result, filters=filters), encoding="utf-8")
    return output_path


def write_walk_forward_nuanced_actions_json(output_path: Path, result: dict[str, Any]) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return output_path
