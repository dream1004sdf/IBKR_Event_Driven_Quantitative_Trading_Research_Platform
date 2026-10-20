from __future__ import annotations

from datetime import UTC, datetime

from recorder.analysis_database import AnalysisDatabase
from recorder.market_outcomes import IntradayBar
from recorder.option_structure_outcomes import (
    OPTION_STRUCTURE_OUTCOME_VERSION,
    OptionStructureOutcomeLogger,
    option_structure_outcome_markdown,
)


class FakeIntradaySource:
    name = "fake_intraday"

    def __init__(self, bars_by_symbol: dict[str, list[IntradayBar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.closed = False

    def get_intraday_bars(self, symbol: str, start: datetime, end: datetime) -> list[IntradayBar]:
        return [
            bar
            for bar in self.bars_by_symbol.get(symbol.upper(), [])
            if start <= bar.timestamp <= end
        ]

    def close(self) -> None:
        self.closed = True


def _sample_options_market_structure() -> dict:
    return {
        "options_market_structure_version": "options-market-structure-desk-v2-hardening",
        "generated_at": "2026-07-01T14:30:05+00:00",
        "source_quote_snapshot_version": "ibkr-option-quote-snapshot-test",
        "source_quote_generated_at": "2026-07-01T14:30:00+00:00",
        "symbols": [
            {
                "symbol": "QQQ",
                "spot": 500.0,
                "quote_count": 12,
                "strike_count": 6,
                "gamma_regime": "negative_gamma_trend_amplification_bias",
                "total_gamma_exposure": -1250000.0,
                "gamma_flip": 498.0,
                "call_wall": 505.0,
                "put_wall": 495.0,
                "max_pain": 500.0,
                "distance_to_pin_pct": 0.0,
                "pin_risk": True,
                "proximity": {"max_pain_distance_pct": 0.0},
                "proximity_alerts": ["near_pin_level"],
                "expiry_buckets": [
                    {
                        "bucket": "0dte",
                        "quote_count": 8,
                        "min_dte": 0,
                        "max_dte": 0,
                        "gamma_regime": "negative_gamma_trend_amplification_bias",
                        "total_gamma_exposure": -900000.0,
                        "call_wall": 505.0,
                        "put_wall": 495.0,
                        "max_pain": 500.0,
                        "pin_risk": True,
                        "proximity_alerts": ["near_pin_level"],
                    }
                ],
                "confidence": "high",
                "confidence_score": 88.0,
                "warnings": [],
            }
        ],
    }


def test_m222_option_structure_outcome_logger_persists_intraday_outcomes(tmp_path):
    db = AnalysisDatabase(tmp_path / "analysis.db")
    bars = [
        IntradayBar("QQQ", datetime(2026, 7, 1, 14, 29, tzinfo=UTC), 500.0),
        IntradayBar("QQQ", datetime(2026, 7, 1, 14, 35, tzinfo=UTC), 503.0),
        IntradayBar("QQQ", datetime(2026, 7, 1, 14, 45, tzinfo=UTC), 501.0),
        IntradayBar("QQQ", datetime(2026, 7, 1, 15, 0, tzinfo=UTC), 497.0),
        IntradayBar("QQQ", datetime(2026, 7, 1, 20, 0, tzinfo=UTC), 499.0),
    ]
    source = FakeIntradaySource({"QQQ": bars})

    result = OptionStructureOutcomeLogger(db, source).build(
        options_market_structure=_sample_options_market_structure(),
        horizons="5M,15M,30M,EOD",
        flat_threshold=0.001,
    )

    assert result["option_structure_outcome_version"] == OPTION_STRUCTURE_OUTCOME_VERSION
    assert result["symbol_count"] == 1
    assert result["completed"] == 4
    assert result["blocked"] == 0
    assert source.closed is True
    rows = db.recent_option_structure_outcome_logs(limit=10, symbol="QQQ", price_source="fake_intraday")
    assert len(rows) == 4
    by_horizon = {row["horizon"]: row for row in rows}
    assert by_horizon["5M"]["direction_label"] == "up"
    assert by_horizon["30M"]["direction_label"] == "down"
    assert by_horizon["EOD"]["reaction_at"] == "2026-07-01T20:00:00+00:00"
    assert by_horizon["5M"]["gamma_regime"] == "negative_gamma_trend_amplification_bias"
    assert by_horizon["5M"]["max_pain"] == 500.0
    assert "near_pin_level" in by_horizon["5M"]["proximity_alerts_json"]


def test_m222_option_structure_outcome_logger_allows_feature_only_pending_mode(tmp_path):
    db = AnalysisDatabase(tmp_path / "analysis.db")

    result = OptionStructureOutcomeLogger(db, None).build(
        options_market_structure=_sample_options_market_structure(),
        horizons=("5M",),
    )

    assert result["completed"] == 0
    assert result["pending"] == 1
    rows = db.recent_option_structure_outcome_logs(limit=5)
    assert rows[0]["price_status"] == "pending"
    assert "price_source_not_configured" in rows[0]["blocker_reason"]
    assert rows[0]["baseline_price"] == 500.0
    markdown = option_structure_outcome_markdown(result)
    assert "M222 Option-Structure Outcome Log" in markdown
    assert "No broker orders" in markdown


def test_m222_option_structure_outcome_logger_marks_future_reaction_as_pending(tmp_path):
    db = AnalysisDatabase(tmp_path / "analysis.db")
    report = _sample_options_market_structure()
    report["generated_at"] = "2026-07-01T14:30:05+00:00"
    report["source_quote_generated_at"] = "2026-07-01T14:30:00+00:00"
    source = FakeIntradaySource(
        {
            "QQQ": [
                IntradayBar("QQQ", datetime(2026, 7, 1, 14, 30, tzinfo=UTC), 500.0),
            ]
        }
    )

    result = OptionStructureOutcomeLogger(
        db,
        source,
        now=datetime(2026, 7, 1, 14, 31, tzinfo=UTC),
    ).build(
        options_market_structure=report,
        horizons="5M",
        flat_threshold=0.001,
    )

    assert result["completed"] == 0
    assert result["pending"] == 1
    assert result["blocked"] == 0
    rows = db.recent_option_structure_outcome_logs(limit=5)
    assert rows[0]["price_status"] == "pending"
    assert "reaction_target_not_due" in rows[0]["blocker_reason"]
