from __future__ import annotations

from recorder.walk_forward_notes import build_walk_forward_adjustment_notes


def _row(index: int, *, hit: int, timestamp: str) -> dict:
    actual_direction = "up" if hit else "down"
    actual_excess = 0.02 if hit else -0.04
    return {
        "id": index,
        "outcome_id": index,
        "replay_corpus_id": index + 100,
        "symbol": "NVDA",
        "horizon": "1D",
        "replay_timestamp": timestamp,
        "scorer_name": "openai",
        "model_name": "gpt-5.4-mini",
        "scorer_version": "openai-replay-score-v14",
        "predicted_direction": "up",
        "actual_direction": actual_direction,
        "direction_hit": hit,
        "expected_return": 0.01,
        "actual_excess_return": actual_excess,
    }


def test_walk_forward_notes_prior_memory_only_and_reflex_candidate() -> None:
    rows = [
        _row(3, hit=0, timestamp="2026-01-03T14:00:00+00:00"),
        _row(1, hit=0, timestamp="2026-01-01T14:00:00+00:00"),
        _row(2, hit=0, timestamp="2026-01-02T14:00:00+00:00"),
    ]

    def packet_builder(row, signals, db):
        return {
            "overlay": {
                "overlay_state": "asset_specific_shock",
                "overlay_action": "risk_reduce",
                "overlay_score": 0.8,
                "urgency": "critical",
                "emergency": True,
                "matched_signal_count": 1,
                "top_signals": [
                    {
                        "source": "benzinga",
                        "published_at": row["replay_timestamp"],
                        "title": "Breaking: urgent semiconductor supply shock",
                        "region": "taiwan_china",
                        "theme": "semiconductor_supply_chain",
                        "sentiment_label": "negative",
                        "assets": ["NVDA", "SMH"],
                        "emergency": True,
                    }
                ],
            },
            "standing_regime_overlay": {"matched_regime_count": 0, "active_regimes": []},
            "economic_indicator_desk": {"risk_bias": "macro_neutral"},
            "tri_desk_alignment": {"confirmation_score": 0.1, "conflict_score": 0.8},
        }

    result = build_walk_forward_adjustment_notes(
        rows,
        [],
        db=None,
        limit=10,
        min_pattern_observations=2,
        weak_hit_rate=0.45,
        packet_builder=packet_builder,
    )

    notes = result["notes"]
    assert [note["replay_timestamp"] for note in notes] == [
        "2026-01-03T14:00:00+00:00",
        "2026-01-01T14:00:00+00:00",
        "2026-01-02T14:00:00+00:00",
    ]
    assert notes[0]["weak_patterns_before"]
    assert notes[0]["reflex_candidate"] is True
    assert notes[1]["weak_patterns_before"] == []
    assert notes[2]["weak_patterns_before"] == []
    assert result["summary"]["categories"]["emergency_reflex_gap"] == 3
