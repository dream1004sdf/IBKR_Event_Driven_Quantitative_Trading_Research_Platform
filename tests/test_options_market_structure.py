from __future__ import annotations

from recorder.options_market_structure import build_options_market_structure_report, options_market_structure_markdown


def test_options_market_structure_builds_gex_walls_max_pain_and_safety() -> None:
    quote_snapshot = {
        "option_quote_snapshot_version": "ibkr-option-quote-snapshot-v1",
        "generated_at": "2026-07-01T14:00:00+00:00",
        "quotes": [
            {"symbol": "SPY", "expiration": "20260717", "strike": 500.0, "right": "C", "gamma": 0.04, "delta": 0.55, "open_interest": 1000, "volume": 100, "multiplier": 100},
            {"symbol": "SPY", "expiration": "20260717", "strike": 505.0, "right": "C", "gamma": 0.035, "delta": 0.42, "open_interest": 1500, "volume": 120, "multiplier": 100},
            {"symbol": "SPY", "expiration": "20260717", "strike": 495.0, "right": "P", "gamma": 0.03, "delta": -0.35, "open_interest": 2000, "volume": 80, "multiplier": 100},
            {"symbol": "SPY", "expiration": "20260717", "strike": 490.0, "right": "P", "gamma": 0.025, "delta": -0.22, "open_interest": 800, "volume": 60, "multiplier": 100},
        ],
    }
    market_data = {"rows": [{"key": "SPY", "price": 500.0}]}

    report = build_options_market_structure_report(
        option_quote_snapshot=quote_snapshot,
        market_data=market_data,
        fallback_to_volume=True,
        pin_near_pct=1.0,
        max_snapshot_age_minutes=1440.0,
    )
    markdown = options_market_structure_markdown(report)

    assert report["options_market_structure_version"] == "options-market-structure-desk-v2-hardening"
    assert report["status"] == "green"
    assert report["session_guard"]["freshness_status"] == "fresh"
    assert report["quote_count"] == 4
    assert report["symbol_count"] == 1
    assert report["scorer_overlay"]["available"] is True
    assert report["safety"]["ibkr_order_submitted"] is False
    assert report["safety"]["transmit_enabled"] is False
    spy = report["symbols"][0]
    assert spy["symbol"] == "SPY"
    assert spy["spot"] == 500.0
    assert spy["call_wall"] == 505.0
    assert spy["put_wall"] == 495.0
    assert spy["max_pain"] is not None
    assert spy["confidence"] == "high"
    assert spy["confidence_score"] >= 80
    assert spy["exposure_source_counts"]["open_interest"] == 4
    assert spy["expiry_buckets"][0]["bucket"] == "front_month_8_35dte"
    assert "proximity" in spy
    assert "Options Market Structure Desk" in markdown
    assert "Expiry buckets" in markdown
    assert "`SPY`" in markdown


def test_options_market_structure_uses_volume_proxy_when_oi_missing() -> None:
    quote_snapshot = {
        "option_quote_snapshot_version": "ibkr-option-quote-snapshot-v1",
        "generated_at": "2026-07-01T14:00:00+00:00",
        "quotes": [
            {
                "symbol": "QQQ",
                "expiration": "20260717",
                "strike": 500.0,
                "right": "C",
                "volume": 300,
                "multiplier": 100,
                "option_computation": {"MODEL_OPTION": {"gamma": 0.02, "delta": 0.5, "underlying_price": 501.0}},
            },
            {
                "symbol": "QQQ",
                "expiration": "20260717",
                "strike": 495.0,
                "right": "P",
                "volume": 250,
                "multiplier": 100,
                "option_computation": {"MODEL_OPTION": {"gamma": 0.021, "delta": -0.4, "underlying_price": 501.0}},
            },
        ],
    }

    report = build_options_market_structure_report(
        option_quote_snapshot=quote_snapshot,
        max_snapshot_age_minutes=1440.0,
    )
    qqq = report["symbols"][0]

    assert report["status"] == "yellow"
    assert qqq["confidence"] == "medium"
    assert qqq["exposure_source_counts"]["volume_proxy"] == 2
    assert "open_interest_missing_using_volume_proxy" in qqq["warnings"]
    assert "some_contracts_missing_bid_ask" in qqq["warnings"]
