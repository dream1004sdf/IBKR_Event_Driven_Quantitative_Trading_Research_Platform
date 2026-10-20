from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from recorder.evidence_operations import (
    build_evidence_freshness_report,
    evidence_freshness_markdown,
    write_evidence_freshness_json,
)
from recorder.evidence_packet import build_overlay_evidence_packet


class _TinyDb:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _make_freshness_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE news_articles (provider TEXT, headline TEXT, published_at TEXT, received_at TEXT, provider_published_at TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE external_market_signals (source TEXT, title TEXT, published_at TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE standing_market_regimes (regime_key TEXT, family TEXT, regime_name TEXT, directional_bias TEXT, status TEXT, region TEXT, theme TEXT, started_at TEXT, last_seen_at TEXT, resolved_at TEXT, severity_score REAL, confidence REAL, affected_assets_json TEXT, evidence_count INTEGER, latest_titles_json TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE technical_indicators (symbol TEXT, as_of_date TEXT, price_source TEXT, close REAL, return_1d REAL, return_5d REAL, return_20d REAL, sma_20 REAL, sma_50 REAL, macd REAL, macd_signal REAL, rsi_14 REAL, volatility_20 REAL, ingestion_version TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE economic_indicator_observations (canonical_series_id TEXT, observation_date TEXT, value REAL, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO news_articles VALUES ('BZ','Fresh headline','2026-06-30T09:50:00+00:00',NULL,'2026-06-30T09:50:00+00:00','2026-06-30T09:51:00+00:00')"
    )
    conn.execute(
        "INSERT INTO external_market_signals VALUES ('gdelt','Old geopolitical item','2026-06-28T09:00:00+00:00','2026-06-28T09:01:00+00:00','2026-06-28T09:01:00+00:00')"
    )
    conn.execute(
        "INSERT INTO standing_market_regimes VALUES ('ai','technology','AI boom','positive','active','US','AI','2025-01-01','2026-06-29T09:00:00+00:00',NULL,0.8,0.9,'[]',12,'[]','2026-06-29T09:00:00+00:00','2026-06-29T09:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO technical_indicators VALUES ('SPY','2026-06-29','ibkr',500,0.01,0.02,0.03,490,480,1,0.8,55,0.12,'test','2026-06-30T09:00:00+00:00','2026-06-30T09:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO economic_indicator_observations VALUES ('DGS10','2026-06-29',4.2,'2026-06-30T08:00:00+00:00','2026-06-30T08:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def test_evidence_freshness_report_marks_stale_sources_and_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "market_events.db"
    _make_freshness_db(db_path)
    market_data_json = tmp_path / "market.json"
    market_data_json.write_text(
        json.dumps(
            {
                "market_data_desk_version": "market-data-specialist-desk-v1",
                "generated_at": "2026-06-30T09:59:30+00:00",
                "status": "green",
                "summary": {"row_count": 2, "status": "green"},
            }
        ),
        encoding="utf-8",
    )
    economic_json = tmp_path / "economic.json"
    economic_json.write_text(
        json.dumps(
            {
                "desk_version": "economic-indicator-desk-v1",
                "cutoff_date": "2026-06-29",
                "covered_series_count": 10,
                "series_count": 12,
                "freshness_counts": {"fresh": 10},
                "indicators": [{"latest_date": "2026-06-29"}],
            }
        ),
        encoding="utf-8",
    )

    report = build_evidence_freshness_report(
        database_path=db_path,
        market_data_json_path=market_data_json,
        economic_desk_json_path=economic_json,
        now="2026-06-30T10:00:00+00:00",
    )
    markdown = evidence_freshness_markdown(report)
    written = write_evidence_freshness_json(tmp_path / "freshness.json", report)

    assert report["evidence_freshness_desk_version"] == "evidence-freshness-desk-v2-marketaux-crosscheck-no-alpha"
    assert report["evidence_gap_warning"] is True
    assert "geopolitical_signals" in report["stale_source_keys"]
    assert report["source_freshness_weights"]["market_news"] == 1.0
    assert report["scorer_guard"]["action"] == "downweight_stale_sources"
    assert "Evidence Freshness Desk" in markdown
    assert written.exists()


def test_evidence_freshness_uses_fresh_marketaux_as_market_news_crosscheck(tmp_path: Path) -> None:
    db_path = tmp_path / "marketaux-overlay.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE news_articles (provider TEXT, headline TEXT, published_at TEXT, received_at TEXT, provider_published_at TEXT, created_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE external_market_signals (source TEXT, title TEXT, published_at TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE standing_market_regimes (regime_key TEXT, family TEXT, regime_name TEXT, directional_bias TEXT, status TEXT, region TEXT, theme TEXT, started_at TEXT, last_seen_at TEXT, resolved_at TEXT, severity_score REAL, confidence REAL, affected_assets_json TEXT, evidence_count INTEGER, latest_titles_json TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE technical_indicators (symbol TEXT, as_of_date TEXT, price_source TEXT, close REAL, return_1d REAL, return_5d REAL, return_20d REAL, sma_20 REAL, sma_50 REAL, macd REAL, macd_signal REAL, rsi_14 REAL, volatility_20 REAL, ingestion_version TEXT, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "CREATE TABLE economic_indicator_observations (canonical_series_id TEXT, observation_date TEXT, value REAL, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO news_articles VALUES ('BZ','Lagging broker headline','2026-06-30T08:30:00+00:00',NULL,'2026-06-30T08:30:00+00:00','2026-06-30T08:31:00+00:00')"
    )
    conn.execute(
        "INSERT INTO external_market_signals VALUES ('marketaux','Fresh cross-provider signal','2026-06-30T09:35:00+00:00','2026-06-30T09:36:00+00:00','2026-06-30T09:36:00+00:00')"
    )
    conn.commit()
    conn.close()

    report = build_evidence_freshness_report(
        database_path=db_path,
        market_data_json_path=None,
        economic_desk_json_path=None,
        now="2026-06-30T10:00:00+00:00",
    )
    rows = {row["source_key"]: row for row in report["sources"]}

    assert rows["market_news"]["status"] == "lagging"
    assert rows["market_news"]["scorer_usability"] == "downweight_latest_claims_marketaux_crosscheck_available"
    assert rows["market_news"]["scorer_weight"] == 0.85
    assert rows["market_news"]["warning"] is None
    assert rows["market_news"]["details"]["marketaux_auxiliary_freshness"]["applied"] is True
    assert "Alpha" not in " ".join(rows["market_news"]["remediation"])


def test_evidence_packet_includes_point_in_time_gap_guard(tmp_path: Path) -> None:
    db_path = tmp_path / "packet.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE fred_observations (id INTEGER PRIMARY KEY, series_id TEXT, observation_date TEXT, value REAL, realtime_start TEXT, realtime_end TEXT, ingestion_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE technical_indicators (id INTEGER PRIMARY KEY, symbol TEXT, as_of_date TEXT, price_source TEXT, close REAL, return_1d REAL, return_5d REAL, return_20d REAL, sma_20 REAL, sma_50 REAL, macd REAL, macd_signal REAL, rsi_14 REAL, volatility_20 REAL, ingestion_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE economic_indicator_observations (id INTEGER PRIMARY KEY, canonical_series_id TEXT, provider TEXT, native_series_id TEXT, observation_date TEXT, value REAL, unit TEXT, frequency TEXT, provider_priority INTEGER, realtime_start TEXT, realtime_end TEXT, ingestion_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE standing_market_regimes (regime_key TEXT, family TEXT, regime_name TEXT, directional_bias TEXT, status TEXT, region TEXT, theme TEXT, started_at TEXT, last_seen_at TEXT, resolved_at TEXT, severity_score REAL, confidence REAL, affected_assets_json TEXT, evidence_count INTEGER, latest_titles_json TEXT, threshold_profile_json TEXT)"
    )
    conn.commit()
    conn.close()

    row = {
        "symbol": "SPY",
        "predicted_direction": "up",
        "event_type": "news",
        "horizon": "1D",
        "replay_timestamp": "2026-06-30T10:00:00+00:00",
    }
    signals = [
        {
            "title": "Old external risk",
            "published_at": "2026-06-25T10:00:00+00:00",
            "region": "Middle East",
            "theme": "conflict",
            "sentiment_label": "negative",
            "assets": ["VIX"],
        }
    ]

    packet = build_overlay_evidence_packet(row, signals, _TinyDb(db_path))

    assert packet["evidence_gap_guard"]["guard_version"] == "scorer-evidence-gap-guard-v1"
    assert packet["evidence_gap_guard"]["evidence_gap_warning"] is True
    assert "Evidence freshness guard" in packet["scorer_injection_text"]


def test_evidence_packet_includes_m221_option_structure_context(tmp_path: Path) -> None:
    db_path = tmp_path / "packet-options.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE fred_observations (id INTEGER PRIMARY KEY, series_id TEXT, observation_date TEXT, value REAL, realtime_start TEXT, realtime_end TEXT, ingestion_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE technical_indicators (id INTEGER PRIMARY KEY, symbol TEXT, as_of_date TEXT, price_source TEXT, close REAL, return_1d REAL, return_5d REAL, return_20d REAL, sma_20 REAL, sma_50 REAL, macd REAL, macd_signal REAL, rsi_14 REAL, volatility_20 REAL, ingestion_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE economic_indicator_observations (id INTEGER PRIMARY KEY, canonical_series_id TEXT, provider TEXT, native_series_id TEXT, observation_date TEXT, value REAL, unit TEXT, frequency TEXT, provider_priority INTEGER, realtime_start TEXT, realtime_end TEXT, ingestion_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE standing_market_regimes (regime_key TEXT, family TEXT, regime_name TEXT, directional_bias TEXT, status TEXT, region TEXT, theme TEXT, started_at TEXT, last_seen_at TEXT, resolved_at TEXT, severity_score REAL, confidence REAL, affected_assets_json TEXT, evidence_count INTEGER, latest_titles_json TEXT, threshold_profile_json TEXT)"
    )
    conn.commit()
    conn.close()

    row = {
        "symbol": "QQQ",
        "actual_subject_symbol": "QQQ",
        "predicted_direction": "up",
        "event_type": "breaking_news",
        "horizon": "1D",
        "replay_timestamp": "2026-07-01T14:00:00+00:00",
        "m220_market_move_option_structure_overlay": {
            "version": "m220-market-move-option-structure-overlay-v1",
            "overall_status": "option_structure_confirms_trend_risk",
            "structure_freshness_status": "green",
            "mapped_move_count": 1,
            "rows": [
                {
                    "market_key": "QQQ",
                    "family_label": "Nasdaq 100",
                    "move_magnitude_pct": 1.2,
                    "matched_option_symbol": "QQQ",
                    "selected_source_type": "proxy",
                    "coverage_status": "proxy_only",
                    "gamma_regime": "negative_gamma_trend_amplification_bias",
                    "call_wall": 530.0,
                    "put_wall": 510.0,
                    "max_pain": 520.0,
                    "proximity_alerts": ["near_call_wall"],
                    "explanation_status": "confirmed_trend_amplification",
                }
            ],
        },
    }

    packet = build_overlay_evidence_packet(row, [], _TinyDb(db_path))

    option_context = packet["option_structure_context"]
    assert option_context["available"] is True
    assert option_context["scorer_action"] == "allow_larger_reaction_if_news_and_market_move_confirm"
    assert option_context["gamma_regime"] == "negative_gamma_trend_amplification_bias"
    assert any(vote["desk"] == "options_market_structure" for vote in packet["tri_desk_alignment"]["desk_votes"])
    assert "Options Market Structure" in packet["scorer_injection_text"]
    assert any("negative-gamma" in item for item in packet["scorer_guidance"])
