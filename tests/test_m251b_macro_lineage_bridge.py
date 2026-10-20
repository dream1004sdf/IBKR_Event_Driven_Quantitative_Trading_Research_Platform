from __future__ import annotations

from recorder.analysis_database import AnalysisDatabase
from recorder.fred_sources import FredObservation
from recorder.international_macro_sources import EconomicIndicatorObservation
from recorder.macro_event_lineage import rebuild_macro_event_lineage_from_observations


def test_fred_observations_rebuild_into_macro_lineage_without_fake_news(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-fred-bridge.db")
    db.upsert_fred_observation(
        FredObservation(
            series_id="CPIAUCSL",
            observation_date="2025-01-01",
            value=319.0,
            source_url="https://fred.stlouisfed.org/series/CPIAUCSL",
            ingestion_version="fred-observations-v1",
        )
    )

    payload = rebuild_macro_event_lineage_from_observations(
        db.path,
        source="fred",
        start="2025-01-01",
        end="2025-01-01",
        universe="global_core",
    )

    with db.connect() as conn:
        raw_news_count = conn.execute("SELECT COUNT(*) AS c FROM raw_news_articles").fetchone()["c"]
        release = conn.execute("SELECT * FROM macro_event_releases").fetchone()
        observation = conn.execute("SELECT * FROM macro_observations").fetchone()
        cluster = conn.execute(
            "SELECT * FROM news_event_clusters WHERE event_cluster_id=?",
            (release["event_cluster_id"],),
        ).fetchone()

    assert payload["input_rows_seen"] == {"fred": 1}
    assert raw_news_count == 0
    assert release["source_system"] == "fred"
    assert release["release_name"] == "US CPI all items"
    assert release["release_time_quality"] == "observation_date_only"
    assert release["reaction_trigger_eligible"] == 0
    assert release["intraday_label_eligible"] == 0
    assert observation["series_id"] == "CPIAUCSL"
    assert observation["vintage_quality"] == "revised_latest_only"
    assert cluster is None


def test_direct_economic_observations_rebuild_into_macro_lineage(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-direct-bridge.db")
    db.upsert_economic_indicator_observation(
        EconomicIndicatorObservation(
            canonical_series_id="KORCPALTT01CTGYM",
            provider="kosis",
            native_series_id="kosis-cpi",
            observation_date="2025-02-01",
            value=2.7,
            unit="pct",
            frequency="M",
            region="Korea",
            category="inflation",
            source_url="https://kosis.kr/",
            ingestion_version="kosis-economic-indicators-v1",
            raw={"fixture": True},
        )
    )

    payload = rebuild_macro_event_lineage_from_observations(
        db.path,
        source="economic_indicators",
        start="2025-02-01",
        end="2025-02-01",
        universe="global_core",
    )

    with db.connect() as conn:
        raw_news_count = conn.execute("SELECT COUNT(*) AS c FROM raw_news_articles").fetchone()["c"]
        release = conn.execute("SELECT * FROM macro_event_releases").fetchone()
        observation = conn.execute("SELECT * FROM macro_observations").fetchone()
        cluster = conn.execute(
            "SELECT * FROM news_event_clusters WHERE event_cluster_id=?",
            (release["event_cluster_id"],),
        ).fetchone()

    assert payload["input_rows_seen"] == {"economic_indicators": 1}
    assert raw_news_count == 0
    assert release["source_system"] == "kosis"
    assert release["country"] == "KR"
    assert release["currency"] == "KRW"
    assert observation["series_id"] == "KORCPALTT01CTGYM"
    assert observation["vintage_quality"] == "provider_observation_date_only"
    assert release["release_time_quality"] == "observation_date_only"
    assert release["reaction_trigger_eligible"] == 0
    assert release["intraday_label_eligible"] == 0
    assert cluster is None
