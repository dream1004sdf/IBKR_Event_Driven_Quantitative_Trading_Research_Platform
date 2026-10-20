from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from recorder.cross_provider_event_roles import (
    CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP,
    CrossProviderEventRoleConfig,
    build_cross_provider_event_roles,
    classify_event_role,
)

TEST_NOW = "2026-07-02T12:00:00+00:00"


def _create_event_db(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute(
        """
        CREATE TABLE news_articles (
            id INTEGER PRIMARY KEY,
            provider TEXT,
            primary_symbol TEXT,
            headline TEXT,
            published_at TEXT,
            received_at TEXT,
            provider_published_at TEXT,
            extra_data TEXT
        )
        """
    )
    con.execute(
        """
        CREATE TABLE external_market_signals (
            id INTEGER PRIMARY KEY,
            source TEXT,
            title TEXT,
            published_at TEXT,
            sentiment_score REAL,
            sentiment_label TEXT,
            assets_json TEXT
        )
        """
    )
    return con


def test_classify_event_role_handles_fast_geo_and_rating() -> None:
    geo = classify_event_role("Israel-Iran tensions lift oil prices near Hormuz", provider="BZ")
    assert geo["event_role"] == "breaking_geopolitical"
    assert "geopolitical" in geo["all_roles"]

    rating = classify_event_role("Goldman Sachs Maintains Buy on Nvidia, Raises Price Target", provider="DJ-N")
    assert rating["event_role"] == "analyst_rating"
    assert rating["direction"] == "positive"


def test_build_cross_provider_event_roles_confirms_cluster(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = _create_event_db(db_path)
    t = "2026-07-02T10:00:00+00:00"
    con.execute("INSERT INTO news_articles VALUES (1,'BZ','NVDA','Nvidia shares jump on AI chip demand',?,?,?,?)", (t, t, t, ""))
    con.execute("INSERT INTO news_articles VALUES (2,'DJ-RT','NVDA','Nvidia shares rise as AI chip demand lifts sector',?,?,?,?)", (t, t, t, ""))
    con.execute("INSERT INTO external_market_signals VALUES (1,'marketaux','Nvidia AI chip demand remains positive',?,0.45,'positive','[\"NVDA\"]')", (t,))
    con.commit()
    con.close()

    payload = build_cross_provider_event_roles(
        CrossProviderEventRoleConfig(
            database_path=db_path,
            now=TEST_NOW,
            lookback_minutes=60 * 24,
            cluster_window_minutes=60,
        )
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["classified_event_count"] == 3
    assert any(cluster["provider_agreement"] == "confirmed" for cluster in payload["clusters"])


def test_marketaux_noise_does_not_become_confirmed(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = _create_event_db(db_path)
    t = "2026-07-02T10:00:00+00:00"
    con.execute("INSERT INTO external_market_signals VALUES (1,'marketaux','Sensex and Nifty rise as local BSE shares gain',?,-0.05,'neutral','[]')", (t,))
    con.commit()
    con.close()

    payload = build_cross_provider_event_roles(
        CrossProviderEventRoleConfig(
            database_path=db_path,
            now=TEST_NOW,
            lookback_minutes=60 * 24,
            cluster_window_minutes=60,
        )
    )

    assert payload["clusters"][0]["provider_agreement"] == "marketaux_noise_filtered"
    assert payload["clusters"][0]["confidence_adjustment"] == 0.0


def test_alpha_vantage_external_rows_are_excluded_from_active_training(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = _create_event_db(db_path)
    t = "2026-07-02T10:00:00+00:00"
    con.execute("INSERT INTO external_market_signals VALUES (1,'alpha_vantage','Alpha-only stale sentiment item',?,0.8,'positive','[\"SPY\"]')", (t,))
    con.commit()
    con.close()

    payload = build_cross_provider_event_roles(
        CrossProviderEventRoleConfig(
            database_path=db_path,
            now=TEST_NOW,
            lookback_minutes=60 * 24,
            cluster_window_minutes=60,
        )
    )

    assert payload["version"] == "m236-cross-provider-event-role-v5-no-alpha-active-training"
    assert payload["summary"]["classified_event_count"] == 0
    assert "alpha_vantage" not in payload["summary"]["provider_family_counts"]


def test_conflict_is_capped_and_exposes_provider_families(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = _create_event_db(db_path)
    t = "2026-07-02T10:00:00+00:00"
    con.execute("INSERT INTO news_articles VALUES (1,'BZ','NVDA','Nvidia shares jump on strong AI chip demand',?,?,?,?)", (t, t, t, ""))
    con.execute("INSERT INTO news_articles VALUES (2,'DJ-N','NVDA','Nvidia shares drop on weak chip margin pressure',?,?,?,?)", (t, t, t, ""))
    con.commit()
    con.close()

    payload = build_cross_provider_event_roles(
        CrossProviderEventRoleConfig(
            database_path=db_path,
            now=TEST_NOW,
            lookback_minutes=60 * 24,
            cluster_window_minutes=60,
        )
    )
    conflict = next(cluster for cluster in payload["clusters"] if cluster["provider_agreement"] == "conflict")

    assert conflict["provider_families"] == ["benzinga", "dow_jones"]
    assert conflict["provider_disagreement"] is True
    assert CONFIDENCE_ADJUSTMENT_NEGATIVE_CAP <= conflict["confidence_adjustment"] < 0


def test_learning_ready_fields_and_outcome_log_for_broadtape_inference(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    outcome_log = tmp_path / "m236_outcomes.jsonl"
    con = _create_event_db(db_path)
    t = "2026-07-02T10:00:00+00:00"
    extra = json.dumps({"asset_mapping": {"target_candidates": ["NVDA"]}})
    con.execute("INSERT INTO news_articles VALUES (1,'BZ','BROADTAPE:ALL','Nvidia shares jump on AI chip demand',?,?,?,?)", (t, t, t, extra))
    con.execute("INSERT INTO news_articles VALUES (2,'DJ-N','NVDA','Nvidia shares rise as AI chip demand lifts sector',?,?,?,?)", (t, t, t, ""))
    con.execute("INSERT INTO external_market_signals VALUES (1,'marketaux','Nvidia AI chip demand remains positive',?,0.45,'positive','[\"NVDA\"]')", (t,))
    con.commit()
    con.close()

    payload = build_cross_provider_event_roles(
        CrossProviderEventRoleConfig(
            database_path=db_path,
            now=TEST_NOW,
            lookback_minutes=60 * 24,
            cluster_window_minutes=60,
            outcome_log_jsonl_path=outcome_log,
            write_outcome_log=True,
        )
    )
    cluster = next(item for item in payload["clusters"] if item["provider_agreement"] == "confirmed")

    assert cluster["canonical_event_id"]
    assert cluster["provider_weighted_confirmation_score"] > 0.5
    assert cluster["average_ticker_inference_confidence"] > 0.5
    assert cluster["sentiment_role_counts"]["primary_event_source"] >= 2
    assert cluster["sentiment_role_counts"]["sentiment_crosscheck"] >= 1
    assert cluster["confidence_adjustment"] <= cluster["adaptive_confidence_cap"]["positive_max"]
    assert "NVDA" in cluster["shared_assets"]
    assert cluster["target_asset_mapping"]["direct_assets"]
    assert payload["outcome_learning_log"]["records_appended"] >= 1
    logged = [json.loads(line) for line in outcome_log.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["status"] == "pending_market_outcome"
    assert "NVDA" in logged[0]["target_assets"]
    assert logged[0]["target_asset_mapping"]["mapping_version"] == "m236-cluster-asset-proxy-v1"
    assert logged[0]["source_titles"]
    assert logged[0]["horizons"] == ["5M", "15M", "30M", "EOD"]


def test_broadtape_without_stored_mapping_gets_broad_market_proxy(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    outcome_log = tmp_path / "m236_outcomes.jsonl"
    con = _create_event_db(db_path)
    t = "2026-07-02T10:00:00+00:00"
    con.execute("INSERT INTO news_articles VALUES (1,'BZ','BROADTAPE:BZ','Stocks rise as Wall Street opens higher',?,?,?,?)", (t, t, t, ""))
    con.commit()
    con.close()

    payload = build_cross_provider_event_roles(
        CrossProviderEventRoleConfig(
            database_path=db_path,
            now=TEST_NOW,
            lookback_minutes=60 * 24,
            cluster_window_minutes=60,
            outcome_log_jsonl_path=outcome_log,
            write_outcome_log=True,
        )
    )

    assert payload["clusters"][0]["canonical_event"]["canonical_asset"] == "SPY"
    assert "SPY" in payload["clusters"][0]["shared_assets"]
    logged = [json.loads(line) for line in outcome_log.read_text(encoding="utf-8").splitlines()]
    assert logged[0]["target_assets"][0] == "SPY"
