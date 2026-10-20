from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

from recorder.analysis_database import AnalysisDatabase
import recorder.external_market_sources as external_market_sources
from recorder.external_market_sources import (
    BREAKING_NEWS_SOURCE,
    _make_signal,
    _normalize_alpha_time,
    _normalize_gdelt_time,
    _redact_error,
    assets_for_region_theme,
    build_breaking_news_signal,
    breaking_news_packet,
    classify_region_theme,
    format_external_market_signal_report,
    marketaux_signal_quality_decision,
    ExternalMarketSignalCollector,
)
from recorder.market_overlay import overlay_for_score


def test_geopolitical_region_theme_asset_mapping():
    region, theme = classify_region_theme(
        "Taiwan tensions rise after new export controls hit advanced semiconductor chips"
    )
    assert region == "taiwan_china"
    assert theme == "semiconductor_supply_chain"
    assert assets_for_region_theme(region, theme) == ("TSM", "SMH", "SOXX", "NVDA", "QQQ")

    region, theme = classify_region_theme("Missile attack near Hormuz lifts oil and tanker risk")
    assert region == "middle_east"
    assert theme == "energy_supply"
    assert "USO" in assets_for_region_theme(region, theme)

    region, theme = classify_region_theme("NVDA option income ETF announces weekly distribution")
    assert region == "global"
    assert theme == "market_news"
    assert "SPY" in assets_for_region_theme(region, theme)

    region, theme = classify_region_theme("Poland says NATO jets respond after Russian drones enter airspace")
    assert region == "russia_ukraine"
    assert theme == "military_escalation"
    assert "XAR" in assets_for_region_theme(region, theme)

    region, theme = classify_region_theme("Cyberattack hits energy infrastructure and rattles market risk appetite")
    assert region == "global"
    assert theme == "cybersecurity"
    assert "CIBR" in assets_for_region_theme(region, theme)

    assert _normalize_alpha_time("20260628T050918") == "2026-06-28T05:09:18+00:00"
    assert _normalize_gdelt_time("20250103T231500Z") == "2025-01-03T23:15:00+00:00"
    assert "SECRET" not in _redact_error("We have detected your API key as SECRET123")


def test_external_market_signal_storage_and_prediction_gate(tmp_path):
    db = AnalysisDatabase(tmp_path / "external-signals.db")
    db.upsert_technical_indicator(
        SimpleNamespace(
            symbol="TSM",
            as_of_date="2026-06-26",
            price_source="fixture",
            close=200.0,
            return_1d=-0.02,
            return_5d=-0.05,
            return_20d=0.01,
            sma_20=205.0,
            sma_50=190.0,
            ema_12=202.0,
            ema_26=198.0,
            macd=-1.2,
            macd_signal=-0.4,
            rsi_14=42.0,
            volatility_20=0.35,
            source_start_date="2026-05-01",
            source_end_date="2026-06-26",
            ingestion_version="fixture",
        )
    )

    signal = _make_signal(
        source="marketaux",
        title="Taiwan tensions rise as export controls hit advanced semiconductor chips",
        url="https://example.test/taiwan",
        published_at="2026-06-27T12:00:00+00:00",
        sentiment_score=-0.62,
        confidence=0.72,
        raw={"uuid": "fixture-1"},
        db=db,
    )
    signal_id, created = db.upsert_external_market_signal(signal)
    assert created is True
    assert signal_id > 0

    row = db.recent_external_market_signals(limit=1)[0]
    assert row["source"] == "marketaux"
    assert row["region"] == "taiwan_china"
    assert row["theme"] == "semiconductor_supply_chain"
    assert json.loads(row["assets_json"])[:3] == ["TSM", "SMH", "SOXX"]
    assert row["market_reaction_status"] == "available"
    assert row["prediction_gate_status"] == "eligible"

    report = format_external_market_signal_report([row])
    assert "marketaux" in report
    assert "taiwan_china" in report


def test_marketaux_backfill_query_does_not_pollute_classification(tmp_path):
    db = AnalysisDatabase(tmp_path / "marketaux-noise.db")
    raw = {
        "uuid": "noise-1",
        "backfill_query": "Iran Israel Middle East oil war",
        "description": "A hedge fund portfolio article with analyst stock picks.",
    }

    skip, flags, multiplier = marketaux_signal_quality_decision(
        "Peconic Partners Stock Portfolio: 9 Stock Picks",
        raw,
    )
    signal = _make_signal(
        source="marketaux",
        title="Peconic Partners Stock Portfolio: 9 Stock Picks",
        url="https://example.test/noise",
        published_at="2026-06-27T12:00:00+00:00",
        sentiment_score=0.05,
        confidence=0.72,
        raw=raw,
        db=db,
    )

    assert skip is True
    assert "skip_low_signal_marketaux_noise" in flags
    assert multiplier == 0.0
    assert signal.region == "global"
    assert signal.theme == "market_news"

    skip, flags, multiplier = marketaux_signal_quality_decision(
        "Top 10 Stocks To Buy Before The Jobs Report",
        {
            "uuid": "noise-2",
            "description": "A generic stock-pick article mentions geopolitical uncertainty and FOMC timing.",
        },
    )
    assert skip is True
    assert "skip_low_signal_marketaux_noise" in flags
    assert multiplier == 0.0


def test_marketaux_window_uses_exclusive_next_day_boundary(tmp_path, monkeypatch):
    calls = []

    def fake_fetch(self, **kwargs):
        calls.append(kwargs)
        return {"data": []}

    monkeypatch.setattr(external_market_sources.MarketauxClient, "fetch", fake_fetch)
    db = AnalysisDatabase(tmp_path / "marketaux-window.db")

    result = ExternalMarketSignalCollector(db).collect_marketaux_window(
        api_key="fixture-key",
        search=None,
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        limit=100,
        must_have_entities=True,
        entity_types="equity,index,etf",
        countries="us",
        group_similar=True,
    )

    assert result["requested"] == 0
    assert calls[0]["published_after"] == "2025-01-01T00:00:00"
    assert calls[0]["published_before"] == "2025-01-02T00:00:00"


def test_marketaux_backfill_can_run_single_query_shard(tmp_path):
    db = AnalysisDatabase(tmp_path / "marketaux-single-query.db")
    collector = ExternalMarketSignalCollector(db)
    calls = []

    def fake_collect_marketaux_window(**kwargs):
        calls.append(kwargs)
        return {"requested": 1, "created": 1, "updated": 0, "errors": []}

    collector.collect_marketaux_window = fake_collect_marketaux_window  # type: ignore[method-assign]

    result = collector.backfill(
        source="marketaux",
        start=date(2025, 1, 1),
        end=date(2025, 1, 1),
        window_days=1,
        limit_per_window=100,
        api_key="fixture-key",
        symbols="SPY,QQQ",
        marketaux_mode="query",
        marketaux_query_name="global_cyber",
    )

    assert result["created"] == 1
    assert [call["query_name"] for call in calls] == ["global_cyber"]
    assert result["executed"][0]["query"] == "global_cyber"


def test_gdelt_collector_is_disabled_for_active_operations(tmp_path):
    db = AnalysisDatabase(tmp_path / "gdelt-disabled.db")
    collector = ExternalMarketSignalCollector(db)

    result = collector.collect_gdelt(limit=5, timespan="1d")

    assert result["disabled"] is True
    assert result["requested"] == 0
    assert result["created"] == 0
    assert "GDELT is disabled" in result["errors"][0]


def test_benzinga_news_converts_to_external_market_signal(tmp_path):
    db = AnalysisDatabase(tmp_path / "benzinga-external-signals.db")
    headline = "{A:800015:L:en:K:-0.88:C:0.91}U.S. Central Command reports strikes near Iran and Hormuz"
    normalized = "u.s. central command reports strikes near iran and hormuz"
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO news_articles(
                provider,article_id,headline,normalized_headline,headline_hash,body,
                published_at,received_at,provider_published_at,first_seen_at,
                timestamp_quality,timestamp_source,primary_symbol,extra_data,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "BZ",
                "bz-fixture-1",
                headline,
                normalized,
                hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "",
                "2026-06-26T20:38:12+00:00",
                "2026-06-26T20:38:12+00:00",
                "2026-06-26T20:38:12+00:00",
                "2026-06-26T20:39:00+00:00",
                "provider_published",
                "provider_published_at",
                "SPY",
                "{}",
                "2026-06-26T20:39:00+00:00",
            ),
        )

    result = ExternalMarketSignalCollector(db).convert_benzinga_news(
        start=__import__("datetime").date(2026, 6, 26),
        end=__import__("datetime").date(2026, 6, 26),
    )
    rows = db.recent_external_market_signals(source="benzinga", limit=5)

    assert result["created"] == 1
    assert rows[0]["source"] == "benzinga"
    assert rows[0]["region"] == "middle_east"
    assert rows[0]["theme"] in {"energy_supply", "military_escalation"}
    assert rows[0]["sentiment_score"] == -0.88
    assert "SPY" in json.loads(rows[0]["assets_json"])


def test_dow_jones_news_converts_to_external_market_signal(tmp_path):
    db = AnalysisDatabase(tmp_path / "dow-jones-external-signals.db")
    headline = "Defense Contractors Slip on Geopolitical Developments, Possible U.S. Ban on Buybacks"
    normalized = headline.lower()
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO news_articles(
                provider,article_id,headline,normalized_headline,headline_hash,body,
                published_at,received_at,provider_published_at,first_seen_at,
                timestamp_quality,timestamp_source,primary_symbol,extra_data,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "DJ-RTG",
                "dj-fixture-1",
                headline,
                normalized,
                hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
                "",
                "2026-07-03T20:33:38+00:00",
                "2026-07-03T20:33:38+00:00",
                "2026-07-03T20:33:38+00:00",
                "2026-07-03T20:34:00+00:00",
                "provider_published",
                "provider_published_at",
                "ITA",
                "{}",
                "2026-07-03T20:34:00+00:00",
            ),
        )
        raw_news_id = conn.execute("SELECT id FROM news_articles WHERE article_id=?", ("dj-fixture-1",)).fetchone()["id"]

    result = ExternalMarketSignalCollector(db).convert_dow_jones_news(
        start=__import__("datetime").date(2026, 7, 3),
        end=__import__("datetime").date(2026, 7, 3),
    )
    rows = db.recent_external_market_signals(source="dow_jones", limit=5)
    with db.connect() as conn:
        canonical_raw_news_id = conn.execute(
            "SELECT id FROM raw_news_articles WHERE legacy_news_id=?",
            (raw_news_id,),
        ).fetchone()["id"]

    assert result["created"] == 1
    assert rows[0]["source"] == "dow_jones"
    assert rows[0]["region"] in {"global", "russia_ukraine"}
    assert rows[0]["theme"] in {"defense", "trade_conflict", "market_news"}
    assert json.loads(rows[0]["raw_json"])["provider"] == "DJ-RTG"
    assert rows[0]["source_news_id"] == canonical_raw_news_id
    assert "ITA" in json.loads(rows[0]["assets_json"])


def test_breaking_one_line_news_builds_emergency_signal_and_packet(tmp_path):
    db = AnalysisDatabase(tmp_path / "breaking-news.db")
    signal = build_breaking_news_signal(
        db,
        headline="Breaking: missile strike near Hormuz sends oil supply risk higher",
        published_at="2026-06-29T01:02:03+00:00",
        symbols="USO,XLE",
        source_label="ibkr_bz_live",
    )
    packet = breaking_news_packet(signal)

    assert signal.source == BREAKING_NEWS_SOURCE
    assert signal.region == "middle_east"
    assert signal.theme in {"energy_supply", "military_escalation", "shipping_chokepoint"}
    assert signal.prediction_gate_status == "eligible"
    assert signal.raw["emergency"] is True
    assert signal.raw["urgency"]["urgency"] == "critical"
    assert packet["system_action"] == "immediate_scorer_alert"
    assert "USO" in packet["assets"]


def test_breaking_news_ingest_feeds_overlay_fast_path(tmp_path):
    db = AnalysisDatabase(tmp_path / "breaking-news-overlay.db")
    collector = ExternalMarketSignalCollector(db)
    result = collector.ingest_breaking_news(
        headline="Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
        published_at="2026-06-29T10:00:00+00:00",
        symbols="NVDA,SMH",
        source_label="ibkr_bz_live",
    )
    rows = db.recent_external_market_signals(source=BREAKING_NEWS_SOURCE, limit=1)
    row = {
        "symbol": "NVDA",
        "replay_timestamp": "2026-06-29T10:15:00+00:00",
        "predicted_direction": "up",
        "horizon": "30M",
    }
    overlay = overlay_for_score(row, rows)

    assert result["created"] == 1
    assert result["emergency_packet"]["urgency"] == "critical"
    assert overlay["emergency"] is True
    assert overlay["urgency"] == "critical"
    assert overlay["overlay_action"] == "risk_reduce"
    assert overlay["overlay_state"] == "asset_specific_shock"


def test_emergency_breaking_backfill_from_historical_tables(tmp_path):
    db = AnalysisDatabase(tmp_path / "breaking-backfill.db")
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO news_articles(
                provider,article_id,headline,normalized_headline,headline_hash,body,
                published_at,received_at,provider_published_at,first_seen_at,
                timestamp_quality,timestamp_source,primary_symbol,extra_data,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "BZ",
                "bz-breaking-1",
                "{A:1:L:en:K:-0.75:C:0.92}Breaking: missile strike near Hormuz raises oil supply risk",
                "breaking missile strike near hormuz raises oil supply risk",
                hashlib.sha256(b"breaking missile strike near hormuz raises oil supply risk").hexdigest(),
                "",
                "2025-01-05T12:00:00+00:00",
                "2025-01-05T12:00:00+00:00",
                "2025-01-05T12:00:00+00:00",
                "2025-01-05T12:00:01+00:00",
                "provider_published",
                "provider_published_at",
                "USO",
                "{}",
                "2025-01-05T12:00:01+00:00",
            ),
        )
    external_signal = build_breaking_news_signal(
        db,
        headline="Tariff shock hits China semiconductor exports",
        published_at="2025-02-01T10:00:00+00:00",
        symbols="SMH,SOXX",
        source_label="fixture",
    )
    db.upsert_external_market_signal(replace(external_signal, source="marketaux", external_id="fixture-external-1"))

    result = ExternalMarketSignalCollector(db).backfill_emergency_breaking_news(
        start=__import__("datetime").date(2025, 1, 1),
        end=__import__("datetime").date(2025, 12, 31),
        source="all",
        min_urgency="high",
    )
    rows = db.recent_external_market_signals(source=BREAKING_NEWS_SOURCE, limit=10)

    assert result["created"] >= 2
    assert result["skipped_low_urgency"] == 0
    assert {json.loads(row["raw_json"])["backfill_source"] for row in rows} >= {
        "news_articles",
        "external_market_signals",
    }
    assert all(row["prediction_gate_status"] == "eligible" for row in rows)
