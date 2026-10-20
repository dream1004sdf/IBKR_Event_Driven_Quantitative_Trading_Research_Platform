from __future__ import annotations

from recorder.trading_dashboard import _m220_market_move_option_structure_overlay


def test_m220_overlay_marks_old_option_structure_expected_deferred_when_options_closed(monkeypatch) -> None:
    monkeypatch.setattr(
        "recorder.trading_dashboard._current_us_option_session_context",
        lambda: {"session": "closed_or_extended", "is_regular_option_session": False},
    )
    overlay = _m220_market_move_option_structure_overlay(
        market_data={
            "rows": [
                {
                    "key": "QQQ",
                    "group": "us_equity_index_etf",
                    "price": 500.0,
                    "close": 495.0,
                    "gap_pct": 1.01,
                    "tick_move_pct": 0.2,
                    "alerts": ["gap_up:+1.01%"],
                }
            ]
        },
        options_market_structure={
            "generated_at": "2026-07-01T14:00:00+00:00",
            "source_quote_generated_at": "2026-07-01T14:00:00+00:00",
            "status": "yellow",
            "session_guard": {"freshness_status": "fresh", "max_snapshot_age_minutes": 10},
            "symbols": [
                {
                    "symbol": "QQQ",
                    "gamma_regime": "negative_gamma_trend_amplification_bias",
                    "confidence": "medium",
                }
            ],
        },
        options_market_structure_supervisor={
            "completed_at": "2026-07-01T14:00:00+00:00",
            "final_quote_fresh": True,
            "final_effective_max_snapshot_age_minutes": 10,
            "index_option_coverage_matrix": {
                "families": [
                    {
                        "family_id": "nasdaq100",
                        "selected_symbols": ["QQQ"],
                        "selected_source_type": "etf",
                        "coverage_status": "direct",
                    }
                ]
            },
        },
    )

    assert overlay["structure_freshness_status"] == "expected_deferred_stale"
    assert overlay["structure_freshness"]["expected_deferred_stale"] is True
    assert "option_structure_artifact_age_exceeded" in overlay["structure_freshness"]["stale_reasons"]
    assert overlay["rows"][0]["explanation_status"] == "expected_deferred_option_structure_context"
