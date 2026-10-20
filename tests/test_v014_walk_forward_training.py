from __future__ import annotations

from recorder.walk_forward_training import walk_forward_learning_simulation


def _row(index: int, *, hit: int, timestamp: str) -> dict:
    actual_direction = "up" if hit else "down"
    actual_excess = 0.01 if hit else -0.02
    return {
        "outcome_id": index,
        "symbol": "NVDA",
        "horizon": "1D",
        "replay_timestamp": timestamp,
        "predicted_direction": "up",
        "actual_direction": actual_direction,
        "direction_hit": hit,
        "expected_return": 0.01,
        "actual_excess_return": actual_excess,
    }


def test_walk_forward_memory_gate_uses_only_prior_rows() -> None:
    rows = [
        _row(3, hit=1, timestamp="2026-01-03T14:00:00+00:00"),
        _row(1, hit=0, timestamp="2026-01-01T14:00:00+00:00"),
        _row(2, hit=0, timestamp="2026-01-02T14:00:00+00:00"),
    ]

    def packet_builder(row, signals, db):
        return {
            "overlay": {"overlay_state": "normal", "overlay_action": "none", "matched_signal_count": 0},
            "standing_regime_overlay": {
                "matched_regime_count": 1,
                "active_regimes": [
                    {
                        "family": "critical_minerals_geopolitics",
                        "directional_bias": "risk_off",
                    }
                ],
            },
            "economic_indicator_desk": {"risk_bias": "macro_neutral"},
        }

    result = walk_forward_learning_simulation(
        rows,
        [],
        db=None,
        min_pattern_observations=2,
        weak_hit_rate=0.45,
        packet_builder=packet_builder,
    )

    lessons = result["top_lessons"]
    assert result["rows"] == 3
    assert result["tradeable_after_memory"]["directional_rows"] == 2
    assert result["observe_only_after_memory"]["directional_rows"] == 1
    assert any(
        item["recommended_action"] == "observe_only_memory_gate"
        and item["track"] == "standing_regime"
        for item in result["learned_patterns"]
    )
    assert all(item["replay_timestamp"] <= "2026-01-02T14:00:00+00:00" for item in lessons)
