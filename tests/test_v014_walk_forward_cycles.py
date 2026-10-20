from __future__ import annotations

import json

from recorder.walk_forward_cycles import (
    run_walk_forward_nuanced_action_cycles,
    run_walk_forward_training_cycles,
    tune_walk_forward_cycle_thresholds,
)


def _row(index: int, *, timestamp: str, hit: int = 0) -> dict:
    return {
        "id": index,
        "outcome_id": index,
        "symbol": "NVDA",
        "horizon": "1D",
        "replay_timestamp": timestamp,
        "predicted_direction": "up",
        "actual_direction": "up" if hit else "down",
        "direction_hit": hit,
        "expected_return": 0.01,
        "actual_excess_return": 0.02 if hit else -0.03,
    }


def test_cycle_training_restarts_with_prior_threshold_table_only() -> None:
    rows = [
        _row(3, timestamp="2026-01-03T14:00:00+00:00"),
        _row(1, timestamp="2026-01-01T14:00:00+00:00"),
        _row(2, timestamp="2026-01-02T14:00:00+00:00"),
    ]

    def packet_builder(row, signals, db):
        return {
            "overlay": {
                "overlay_state": "asset_specific_shock",
                "overlay_action": "risk_reduce",
                "urgency": "critical",
                "emergency": True,
                "matched_signal_count": 1,
                "top_signals": [{"emergency": True, "title": "Breaking shock"}],
            },
            "standing_regime_overlay": {"matched_regime_count": 0, "active_regimes": []},
            "economic_indicator_desk": {"risk_bias": "macro_neutral"},
        }

    result = run_walk_forward_training_cycles(
        rows,
        [],
        db=None,
        cycles=2,
        min_pattern_observations=2,
        weak_hit_rate=0.45,
        max_threshold_entries=20,
        packet_builder=packet_builder,
    )

    cycle1, cycle2 = result["cycle_results"]
    assert cycle1["input_policy_summary"]["entries"] == 0
    assert cycle1["action_counts"]["directional_signal"] == 2
    assert cycle1["action_counts"]["reflex_observe_only"] == 1
    assert cycle1["output_policy_summary"]["actions"]["reflex_observe_only"] >= 1
    assert cycle2["input_policy_summary"]["actions"]["reflex_observe_only"] >= 1
    assert cycle2["action_counts"]["reflex_observe_only"] == 3
    assert result["final_policy_summary"]["entries"] >= 1
    assert any(entry["action"] == "reflex_observe_only" for entry in result["final_threshold_table"])


def test_threshold_tuner_prefers_candidate_that_passes_coverage() -> None:
    rows = [
        _row(1, timestamp="2026-01-01T14:00:00+00:00"),
        _row(2, timestamp="2026-01-02T14:00:00+00:00"),
        _row(3, timestamp="2026-01-03T14:00:00+00:00"),
        _row(4, timestamp="2026-01-04T14:00:00+00:00", hit=1),
    ]

    def packet_builder(row, signals, db):
        return {
            "overlay": {
                "overlay_state": "asset_specific_shock",
                "overlay_action": "risk_reduce",
                "urgency": "critical",
                "emergency": True,
                "matched_signal_count": 1,
                "top_signals": [{"emergency": True, "title": "Breaking shock"}],
            },
            "standing_regime_overlay": {"matched_regime_count": 0, "active_regimes": []},
            "economic_indicator_desk": {"risk_bias": "macro_neutral"},
        }

    result = tune_walk_forward_cycle_thresholds(
        rows,
        [],
        db=None,
        cycles=2,
        min_pattern_observations_grid=(2, 10),
        weak_hit_rate_grid=(0.45,),
        max_threshold_entries_grid=(20,),
        min_coverage=0.75,
        top_n=4,
        packet_builder=packet_builder,
    )

    assert result["candidates_evaluated"] >= 1
    assert result["selected"]["passes_min_coverage"] is True
    assert result["selected"]["min_pattern_observations"] == 10
    assert result["selected"]["coverage"] == 1.0


def test_threshold_tuner_writes_and_resumes_checkpoint(tmp_path) -> None:
    rows = [
        _row(1, timestamp="2026-01-01T14:00:00+00:00"),
        _row(2, timestamp="2026-01-02T14:00:00+00:00"),
        _row(3, timestamp="2026-01-03T14:00:00+00:00"),
        _row(4, timestamp="2026-01-04T14:00:00+00:00", hit=1),
    ]
    checkpoint = tmp_path / "threshold.checkpoints.jsonl"
    packet_builder_calls: list[int] = []

    def packet_builder(row, signals, db):
        packet_builder_calls.append(int(row["id"]))
        return {
            "overlay": {"overlay_state": "normal", "overlay_action": "none", "matched_signal_count": 0},
            "standing_regime_overlay": {"matched_regime_count": 0, "active_regimes": []},
            "economic_indicator_desk": {"risk_bias": "macro_neutral"},
        }

    result = tune_walk_forward_cycle_thresholds(
        rows,
        [],
        db=None,
        cycles=1,
        min_pattern_observations_grid=(2,),
        weak_hit_rate_grid=(0.45,),
        max_threshold_entries_grid=(20,),
        checkpoint_jsonl_path=checkpoint,
        packet_checkpoint_interval=2,
        packet_builder=packet_builder,
    )
    records = [json.loads(line) for line in checkpoint.read_text(encoding="utf-8").splitlines()]
    assert result["checkpoint"]["newly_evaluated_candidates"] == 1
    assert {item["event"] for item in records} >= {
        "run_started",
        "packet_cache_progress",
        "packet_cached",
        "candidate_started",
        "cycle_completed",
        "candidate_completed",
    }
    assert packet_builder_calls == [1, 2, 3, 4]

    packet_builder_calls.clear()
    resumed = tune_walk_forward_cycle_thresholds(
        rows,
        [],
        db=None,
        cycles=1,
        min_pattern_observations_grid=(2,),
        weak_hit_rate_grid=(0.45,),
        max_threshold_entries_grid=(20,),
        checkpoint_jsonl_path=checkpoint,
        packet_checkpoint_interval=2,
        packet_builder=packet_builder,
    )
    assert resumed["checkpoint"]["resumed_candidates"] == 1
    assert resumed["checkpoint"]["newly_evaluated_candidates"] == 0
    assert resumed["checkpoint"]["cached_packets"] == 4
    assert packet_builder_calls == []


def test_nuanced_actions_keep_confidence_haircut_tradeable() -> None:
    rows = [
        _row(1, timestamp="2026-01-01T14:00:00+00:00", hit=0),
        _row(2, timestamp="2026-01-02T14:00:00+00:00", hit=1),
        _row(3, timestamp="2026-01-03T14:00:00+00:00", hit=0),
        _row(4, timestamp="2026-01-04T14:00:00+00:00", hit=1),
    ]

    def packet_builder(row, signals, db):
        return {
            "overlay": {"overlay_state": "normal", "overlay_action": "none", "matched_signal_count": 0},
            "standing_regime_overlay": {"matched_regime_count": 0, "active_regimes": []},
            "economic_indicator_desk": {"risk_bias": "macro_neutral"},
        }

    result = run_walk_forward_nuanced_action_cycles(
        rows,
        [],
        db=None,
        cycles=2,
        min_pattern_observations=4,
        reflex_hit_rate=0.20,
        second_pass_hit_rate=0.40,
        haircut_hit_rate=0.60,
        neutral_flat_rate=0.80,
        max_threshold_entries=10,
        packet_builder=packet_builder,
    )

    cycle2 = result["cycle_results"][1]
    assert result["final_policy_summary"]["actions"]["confidence_haircut"] >= 1
    assert cycle2["action_counts"]["confidence_haircut"] == 4
    assert cycle2["coverage"] == 1.0
    assert cycle2["tradeable_after_nuanced_action"]["directional_rows"] == 4
