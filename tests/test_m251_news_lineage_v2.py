from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from recorder.analysis_database import AnalysisDatabase
from recorder.database import Database
from recorder.external_market_sources import ExternalMarketSignalCollector, MarketauxClient
from recorder.models import NewsInput
from recorder.news_lineage_v2 import assign_article_to_event_cluster, store_article_asset_mapping


def _insert_news(db: Database, *, headline: str, primary_symbol: str, article_id: str) -> tuple[int, int]:
    now = datetime.now(UTC)
    normalized = " ".join(headline.lower().split())
    legacy_news_id = db.insert_news(
        NewsInput(
            provider="BZ",
            article_id=article_id,
            headline=headline,
            published_at=now,
            received_at=now,
            primary_symbol=primary_symbol,
            extra_data="{}",
            provider_published_at=now,
            first_seen_at=now,
        ),
        normalized,
        hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )
    with db.connect() as conn:
        raw_news_id = int(
            conn.execute(
                "SELECT id FROM raw_news_articles WHERE legacy_news_id=?",
                (legacy_news_id,),
            ).fetchone()["id"]
        )
    return legacy_news_id, raw_news_id


def test_ticker_specific_route_symbol_is_not_content_entity_by_itself(tmp_path):
    db = Database(tmp_path / "m251-route-only.db")
    _, raw_news_id = _insert_news(
        db,
        headline="Fed official says rates may stay higher for longer",
        primary_symbol="NVDA",
        article_id="route-only-1",
    )

    with db.connect() as conn:
        mapping_run_id = store_article_asset_mapping(conn, raw_news_id)
        raw = conn.execute("SELECT * FROM raw_news_articles WHERE id=?", (raw_news_id,)).fetchone()
        job_statuses = [
            str(row["status"])
            for row in conn.execute("SELECT status FROM news_enrichment_jobs").fetchall()
        ]
        candidates = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM news_asset_candidates WHERE mapping_run_id=?",
                (mapping_run_id,),
            ).fetchall()
        ]

    assert raw["source_system"] == "ibkr"
    assert raw["ingest_mode"] == "live"
    assert raw["route_type"] == "ticker_specific"
    assert raw["primary_route_symbol"] == "NVDA"
    assert job_statuses == ["queued"]
    assert any(
        c["symbol"] == "NVDA" and c["role"] == "route_only" and c["tradable_candidate"] == 0
        for c in candidates
    )
    assert not any(
        c["symbol"] == "NVDA" and c["role"] == "primary" and c["tradable_candidate"] == 1
        for c in candidates
    )


def test_content_entity_can_be_added_separately_from_route_symbol(tmp_path):
    db = Database(tmp_path / "m251-content-entity.db")
    _, raw_news_id = _insert_news(
        db,
        headline="Nvidia shares jump as AI chip demand strengthens",
        primary_symbol="NVDA",
        article_id="content-entity-1",
    )

    with db.connect() as conn:
        mapping_run_id = store_article_asset_mapping(conn, raw_news_id)
        candidates = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM news_asset_candidates WHERE mapping_run_id=?",
                (mapping_run_id,),
            ).fetchall()
        ]

    assert any(c["symbol"] == "NVDA" and c["role"] == "route_only" for c in candidates)
    assert any(
        c["symbol"] == "NVDA" and c["role"] == "primary" and c["source"] == "content_entity"
        for c in candidates
    )


def test_broadtape_no_entity_maps_spy_as_context_only_not_target(tmp_path):
    db = Database(tmp_path / "m251-broadtape-context.db")
    _, raw_news_id = _insert_news(
        db,
        headline="Company Announces Strategic Review",
        primary_symbol="BROADTAPE:BZ",
        article_id="broadtape-context-1",
    )

    with db.connect() as conn:
        mapping_run_id = store_article_asset_mapping(conn, raw_news_id)
        raw = conn.execute("SELECT * FROM raw_news_articles WHERE id=?", (raw_news_id,)).fetchone()
        candidates = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM news_asset_candidates WHERE mapping_run_id=?",
                (mapping_run_id,),
            ).fetchall()
        ]

    assert raw["route_type"] == "broadtape"
    assert any(
        c["symbol"] == "SPY"
        and c["role"] == "context_only"
        and c["source"] == "fallback_context"
        and c["tradable_candidate"] == 0
        for c in candidates
    )
    assert not any(c["symbol"] == "SPY" and c["tradable_candidate"] == 1 for c in candidates)


def test_marketaux_raw_is_stored_before_noise_filter_and_links_signal(monkeypatch, tmp_path):
    payload = {
        "data": [
            {
                "uuid": "marketaux-accepted-1",
                "title": "Taiwan export controls hit Nvidia AI chips",
                "url": "https://example.test/accepted",
                "published_at": "2026-07-05T12:00:00Z",
                "description": "Taiwan and China export controls pressure advanced chips.",
                "sentiment_score": -0.42,
                "entities": [
                    {
                        "symbol": "NVDA",
                        "name": "Nvidia",
                        "type": "equity",
                        "exchange": "NASDAQ",
                        "country": "us",
                        "match_score": 0.92,
                        "sentiment_score": -0.4,
                    }
                ],
            },
            {
                "uuid": "marketaux-noise-1",
                "title": "Top 10 Stocks To Buy Before The Jobs Report",
                "url": "https://example.test/noise",
                "published_at": "2026-07-05T12:03:00Z",
                "description": "A generic stock-pick article with analyst ideas.",
                "entities": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple",
                        "type": "equity",
                        "exchange": "NASDAQ",
                        "country": "us",
                        "match_score": 0.80,
                    }
                ],
            },
        ]
    }

    def fake_fetch(self, **kwargs):  # noqa: ANN001
        return payload

    monkeypatch.setattr(MarketauxClient, "fetch", fake_fetch)
    db = AnalysisDatabase(tmp_path / "m251-marketaux.db")

    result = ExternalMarketSignalCollector(db).collect_marketaux(api_key="test", limit=2)

    with db.connect() as conn:
        raw_count = conn.execute("SELECT COUNT(*) AS count FROM raw_news_articles").fetchone()["count"]
        entity_count = conn.execute("SELECT COUNT(*) AS count FROM provider_entities").fetchone()["count"]
        signal = conn.execute("SELECT * FROM external_market_signals WHERE source='marketaux'").fetchone()
        link_count = conn.execute("SELECT COUNT(*) AS count FROM external_signal_source_links").fetchone()["count"]
        raw_modes = {
            row["provider_article_id"]: (row["source_system"], row["route_type"], row["ingest_mode"])
            for row in conn.execute("SELECT * FROM raw_news_articles").fetchall()
        }
        statuses = {
            row["provider_article_id"]: (row["processing_status"], row["skip_reason"])
            for row in conn.execute(
                """SELECT r.provider_article_id,p.processing_status,p.skip_reason
                     FROM raw_news_articles r
                     JOIN raw_news_processing_status p ON p.news_id=r.id
                    ORDER BY r.provider_article_id"""
            ).fetchall()
        }
        visibility = {
            row["provider_article_id"]: (row["model_visible_at_utc"], row["availability_quality"])
            for row in conn.execute(
                """SELECT r.provider_article_id,v.model_visible_at_utc,v.availability_quality
                     FROM raw_news_articles r
                     JOIN news_visibility_policy v ON v.news_id=r.id
                    ORDER BY r.provider_article_id"""
            ).fetchall()
        }
        cluster_links = [
            dict(row)
            for row in conn.execute(
                """SELECT l.*
                     FROM news_article_cluster_links l
                     JOIN raw_news_articles r ON r.id=l.news_id
                    ORDER BY r.provider_article_id"""
            ).fetchall()
        ]

    assert result["created"] == 1
    assert result["skipped_noise"] == 1
    assert raw_count == 2
    assert entity_count == 2
    assert raw_modes["marketaux-accepted-1"] == ("marketaux", "external_api", "polling")
    assert raw_modes["marketaux-noise-1"] == ("marketaux", "external_api", "polling")
    assert signal["source_news_id"] is not None
    assert json.loads(signal["raw_json"])["uuid"] == "marketaux-accepted-1"
    assert link_count == 1
    assert statuses["marketaux-accepted-1"] == ("derived_signal_created", None)
    assert statuses["marketaux-noise-1"] == ("skipped_noise", "skip_low_signal_marketaux_noise")
    assert visibility["marketaux-accepted-1"] == (
        "2026-07-05T12:05:00+00:00",
        "historical_estimated_marketaux_published_plus_300s",
    )
    assert visibility["marketaux-noise-1"] == (
        "2026-07-05T12:08:00+00:00",
        "historical_estimated_marketaux_published_plus_300s",
    )
    assert all(link["link_type"] in {"primary_source", "supporting_source"} for link in cluster_links)
    assert sum(1 for link in cluster_links if link["is_primary_source"] == 1) >= 1


def test_article_asset_mapping_is_idempotent_after_cluster_link(tmp_path):
    db = Database(tmp_path / "m251-idempotent-after-cluster.db")
    _, raw_news_id = _insert_news(
        db,
        headline="Nvidia shares jump on AI chip demand",
        primary_symbol="BROADTAPE:BZ",
        article_id="idempotent-cluster-1",
    )

    with db.connect() as conn:
        first_mapping_run_id = store_article_asset_mapping(conn, raw_news_id)
        assign_article_to_event_cluster(conn, raw_news_id)
        counts_after_cluster = {
            "runs": conn.execute("SELECT COUNT(*) AS count FROM news_asset_mapping_runs").fetchone()["count"],
            "candidates": conn.execute("SELECT COUNT(*) AS count FROM news_asset_candidates").fetchone()["count"],
        }
        second_mapping_run_id = store_article_asset_mapping(conn, raw_news_id)
        counts_after_resume = {
            "runs": conn.execute("SELECT COUNT(*) AS count FROM news_asset_mapping_runs").fetchone()["count"],
            "candidates": conn.execute("SELECT COUNT(*) AS count FROM news_asset_candidates").fetchone()["count"],
        }

    assert second_mapping_run_id == first_mapping_run_id
    assert counts_after_resume == counts_after_cluster
