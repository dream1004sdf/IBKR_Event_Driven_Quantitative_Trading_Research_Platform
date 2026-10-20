from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime

from recorder.analysis_database import AnalysisDatabase
from recorder.external_market_sources import ExternalMarketSignalCollector, MarketauxClient
from recorder.models import NewsInput


def _insert_news(
    db: AnalysisDatabase,
    *,
    provider: str,
    article_id: str,
    headline: str,
    primary_symbol: str,
) -> int:
    now = datetime.now(UTC)
    normalized = " ".join(headline.lower().split())
    return db.insert_news(
        NewsInput(
            provider=provider,
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


def _cluster_roles(db: AnalysisDatabase) -> list[dict]:
    with db.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM news_event_cluster_roles ORDER BY event_cluster_id"
            ).fetchall()
        ]


def test_m252_clusters_bz_dj_same_event_and_suppresses_derived_signal_double_count(tmp_path):
    db = AnalysisDatabase(tmp_path / "m252-confirmation.db")
    _insert_news(
        db,
        provider="BZ",
        article_id="bz-nvda-1",
        headline="Nvidia shares jump on AI chip demand",
        primary_symbol="BROADTAPE:BZ",
    )
    _insert_news(
        db,
        provider="DJNL",
        article_id="dj-nvda-1",
        headline="Nvidia shares rise as AI chip demand lifts sector",
        primary_symbol="NVDA",
    )

    collector = ExternalMarketSignalCollector(db)
    bz_result = collector.convert_benzinga_news(
        start=date(2020, 1, 1),
        end=date(2030, 1, 1),
        keywords_only=False,
    )
    dj_result = collector.convert_dow_jones_news(
        start=date(2020, 1, 1),
        end=date(2030, 1, 1),
        keywords_only=False,
    )

    roles = _cluster_roles(db)
    confirmed = [row for row in roles if row["provider_agreement"] == "confirmed"]

    assert bz_result["created"] == 1
    assert dj_result["created"] == 1
    assert len(confirmed) == 1
    role = confirmed[0]
    assert role["provider_count"] == 2
    assert role["article_count"] == 2
    assert role["source_news_count"] == 2
    assert role["external_signal_count"] == 2
    assert role["provider_confirmation_score"] >= 0.75
    assert set(json.loads(role["provider_families_json"])) == {"benzinga", "dow_jones"}
    assert "NVDA" in json.loads(role["shared_assets_json"])
    assert len(json.loads(role["double_count_suppressed_signal_ids_json"])) == 2

    with db.connect() as conn:
        signal_clusters = [
            row["event_cluster_id"]
            for row in conn.execute("SELECT event_cluster_id FROM external_market_signals").fetchall()
        ]
        candidate_clusters = [
            row["event_cluster_id"]
            for row in conn.execute(
                "SELECT DISTINCT event_cluster_id FROM news_asset_candidates WHERE event_cluster_id IS NOT NULL"
            ).fetchall()
        ]

    assert signal_clusters and all(value == role["event_cluster_id"] for value in signal_clusters)
    assert role["event_cluster_id"] in candidate_clusters


def test_m252_single_raw_plus_derived_signal_does_not_count_as_two_providers(tmp_path):
    db = AnalysisDatabase(tmp_path / "m252-single-source.db")
    _insert_news(
        db,
        provider="BZ",
        article_id="bz-single-1",
        headline="Nvidia shares jump on AI chip demand",
        primary_symbol="NVDA",
    )

    result = ExternalMarketSignalCollector(db).convert_benzinga_news(
        start=date(2020, 1, 1),
        end=date(2030, 1, 1),
        keywords_only=False,
    )
    roles = _cluster_roles(db)

    assert result["created"] == 1
    assert len(roles) == 1
    role = roles[0]
    assert role["provider_agreement"] == "single_source"
    assert role["provider_count"] == 1
    assert role["source_news_count"] == 1
    assert role["external_signal_count"] == 1
    assert json.loads(role["double_count_suppressed_signal_ids_json"])

    with db.connect() as conn:
        item_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT item_type,double_count_status,included_in_confirmation FROM news_event_cluster_items"
            ).fetchall()
        ]

    assert any(row["item_type"] == "raw_news" and row["included_in_confirmation"] == 1 for row in item_rows)
    assert any(
        row["item_type"] == "external_signal"
        and row["double_count_status"] == "derived_duplicate"
        and row["included_in_confirmation"] == 0
        for row in item_rows
    )


def test_m252_marketaux_noise_raw_can_cluster_without_becoming_confirmation(monkeypatch, tmp_path):
    payload = {
        "data": [
            {
                "uuid": "marketaux-noise-m252",
                "title": "Top 10 Stocks To Buy Before The Jobs Report",
                "url": "https://example.test/noise",
                "published_at": "2026-07-05T12:03:00Z",
                "description": "A generic hedge fund portfolio article with analyst stock picks.",
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
            }
        ]
    }

    def fake_fetch(self, **kwargs):  # noqa: ANN001
        return payload

    monkeypatch.setattr(MarketauxClient, "fetch", fake_fetch)
    db = AnalysisDatabase(tmp_path / "m252-marketaux-noise.db")

    result = ExternalMarketSignalCollector(db).collect_marketaux(api_key="test", limit=1)
    roles = _cluster_roles(db)

    assert result["created"] == 0
    assert result["skipped_noise"] == 1
    assert len(roles) == 1
    role = roles[0]
    assert role["provider_agreement"] == "marketaux_noise_filtered"
    assert role["provider_count"] == 1
    assert role["article_count"] == 1
    assert role["external_signal_count"] == 0
    assert role["marketaux_noise_score"] == 1.0
