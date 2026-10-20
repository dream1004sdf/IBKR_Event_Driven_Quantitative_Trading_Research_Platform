from __future__ import annotations

from recorder.analysis_database import AnalysisDatabase
from recorder.macro_event_lineage import (
    MacroEventReleaseInput,
    MacroObservationInput,
    rebuild_macro_event_lineage_from_observations,
    upsert_macro_event_release,
)


def test_macro_release_is_first_class_event_cluster_not_fake_news(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-macro.db")

    with db.connect() as conn:
        macro_event_id = upsert_macro_event_release(
            conn,
            MacroEventReleaseInput(
                source_system="fred",
                release_name="US CPI",
                country="US",
                region="north_america",
                currency="USD",
                event_type="macro_data_release",
                scheduled_at_utc="2026-07-14T12:30:00+00:00",
                released_at_utc="2026-07-14T12:30:00+00:00",
                available_at_utc="2026-07-14T12:30:05+00:00",
                model_visible_at_utc="2026-07-14T12:30:05+00:00",
                release_time_quality="official_exact",
                importance="tier1",
                observations=(
                    MacroObservationInput(
                        series_id="CPIAUCSL",
                        indicator_name="CPI",
                        actual_value=321.0,
                        prior_value=319.0,
                        surprise_raw=2.0,
                        vintage_date="2026-07-14",
                        vintage_quality="alfred_vintage",
                        data_source="alfred",
                        data_version="vintage_2026_07_14",
                    ),
                ),
            ),
        )

        raw_news_count = conn.execute("SELECT COUNT(*) AS c FROM raw_news_articles").fetchone()["c"]
        release = conn.execute("SELECT * FROM macro_event_releases WHERE macro_event_id=?", (macro_event_id,)).fetchone()
        cluster = conn.execute(
            "SELECT * FROM news_event_clusters WHERE event_cluster_id=?",
            (release["event_cluster_id"],),
        ).fetchone()
        observation = conn.execute(
            "SELECT * FROM macro_observations WHERE macro_event_id=?",
            (macro_event_id,),
        ).fetchone()
        link = conn.execute(
            "SELECT * FROM macro_event_cluster_links WHERE macro_event_id=?",
            (macro_event_id,),
        ).fetchone()

    assert raw_news_count == 0
    assert release["source_system"] == "fred"
    assert release["model_visible_at_utc"] == "2026-07-14T12:30:05+00:00"
    assert cluster["cluster_type"] == "macro_release"
    assert cluster["event_class"] == "macro_event.inflation"
    assert cluster["macro_release_count"] == 1
    assert observation["vintage_quality"] == "alfred_vintage"
    assert link["link_type"] == "primary_scheduled_event"


def test_latest_revised_macro_data_can_be_marked_out_of_strict_replay(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-revised-only.db")

    with db.connect() as conn:
        macro_event_id = upsert_macro_event_release(
            conn,
            MacroEventReleaseInput(
                source_system="fred",
                release_name="US Retail Sales",
                country="US",
                currency="USD",
                event_type="macro_data_release",
                scheduled_at_utc="2026-07-16T12:30:00+00:00",
                available_at_utc="2026-07-16T12:35:00+00:00",
                observations=(
                    MacroObservationInput(
                        series_id="RSAFS",
                        indicator_name="Retail Sales",
                        actual_value=100.0,
                        prior_value=99.0,
                        vintage_quality="revised_latest_only",
                        data_source="fred",
                        data_version="latest_revised",
                    ),
                ),
            ),
        )
        observation = conn.execute(
            "SELECT vintage_quality,data_version FROM macro_observations WHERE macro_event_id=?",
            (macro_event_id,),
        ).fetchone()

    assert observation["vintage_quality"] == "revised_latest_only"
    assert observation["data_version"] == "latest_revised"


def test_regime_feature_macro_observation_does_not_create_reaction_cluster(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-regime-feature.db")

    payload = MacroEventReleaseInput(
        source_system="fred",
        release_name="10-Year Treasury Constant Maturity Rate",
        country="US",
        currency="USD",
        event_type="rates_market_observation",
        scheduled_at_utc="2026-07-01T23:59:59+00:00",
        released_at_utc="2026-07-01T23:59:59+00:00",
        available_at_utc="2026-07-01T23:59:59+00:00",
        model_visible_at_utc="2026-07-01T23:59:59+00:00",
        observations=(
            MacroObservationInput(
                series_id="DGS10",
                indicator_name="10-Year Treasury Constant Maturity Rate",
                actual_value=4.25,
                prior_value=4.20,
                surprise_raw=0.05,
                period="2026-07-01",
                vintage_date="2026-07-01",
                vintage_quality="fred_realtime_observation",
                data_source="fred",
                data_version="test",
            ),
        ),
    )

    with db.connect() as conn:
        first_id = upsert_macro_event_release(conn, payload)
        second_id = upsert_macro_event_release(conn, payload)
        release = conn.execute("SELECT * FROM macro_event_releases WHERE macro_event_id=?", (first_id,)).fetchone()
        link_count = conn.execute("SELECT COUNT(*) AS c FROM macro_event_cluster_links").fetchone()["c"]
        cluster_count = conn.execute("SELECT COUNT(*) AS c FROM news_event_clusters WHERE cluster_type='macro_release'").fetchone()["c"]
        release_count = conn.execute("SELECT COUNT(*) AS c FROM macro_event_releases").fetchone()["c"]
        observation_count = conn.execute("SELECT COUNT(*) AS c FROM macro_observations").fetchone()["c"]

    assert second_id == first_id
    assert release["event_cluster_id"] is None
    assert release["reaction_trigger_eligible"] == 0
    assert release["regime_feature_eligible"] == 1
    assert link_count == 0
    assert cluster_count == 0
    assert release_count == 1
    assert observation_count == 1


def test_fomc_seed_creates_central_bank_events_without_fake_news(tmp_path):
    db_path = tmp_path / "m251b-fomc-seed.db"
    AnalysisDatabase(db_path)

    payload = rebuild_macro_event_lineage_from_observations(
        str(db_path),
        source="fomc",
        start="2025-01-01",
        end="2025-01-31",
    )

    with AnalysisDatabase(db_path).connect() as conn:
        central_bank_count = conn.execute("SELECT COUNT(*) AS c FROM central_bank_events").fetchone()["c"]
        raw_news_count = conn.execute("SELECT COUNT(*) AS c FROM raw_news_articles").fetchone()["c"]
        release = conn.execute("SELECT * FROM macro_event_releases").fetchone()

    assert payload["central_bank_events_total"] == 1
    assert central_bank_count == 1
    assert raw_news_count == 0
    assert release["source_system"] == "federal_reserve"
    assert release["event_type"] == "central_bank_decision"
    assert release["reaction_trigger_eligible"] == 1


def test_daily_market_series_are_regime_only_even_if_input_says_macro_release(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-market-series-regime.db")
    with db.connect() as conn:
        ids = []
        for series_id, name in (
            ("DGS10", "US Treasury 10-year yield"),
            ("VIXCLS", "CBOE VIX close"),
            ("DCOILWTICO", "WTI crude oil price"),
        ):
            ids.append(
                upsert_macro_event_release(
                    conn,
                    MacroEventReleaseInput(
                        source_system="fred",
                        release_name=name,
                        country="US",
                        event_type="macro_data_release",
                        scheduled_at_utc="2026-07-01T23:59:59+00:00",
                        released_at_utc="2026-07-01T23:59:59+00:00",
                        available_at_utc="2026-07-01T23:59:59+00:00",
                        model_visible_at_utc="2026-07-01T23:59:59+00:00",
                        release_time_quality="provider_exact",
                        observations=(
                            MacroObservationInput(
                                series_id=series_id,
                                indicator_name=name,
                                period="2026-07-01",
                                vintage_date="2026-07-01",
                                vintage_quality="fred_realtime_observation",
                                data_source="fred",
                                data_version="test",
                            ),
                        ),
                    ),
                )
            )
        rows = conn.execute(
            "SELECT event_type,semantic_type,reaction_trigger_eligible,event_cluster_id FROM macro_event_releases ORDER BY macro_event_id"
        ).fetchall()

    assert all(row["reaction_trigger_eligible"] == 0 for row in rows)
    assert all(row["event_cluster_id"] is None for row in rows)
    assert rows[0]["event_type"] == "rates_market_observation"
    assert rows[1]["event_type"] == "volatility_regime_observation"
    assert rows[2]["event_type"] == "energy_price_observation"


def test_allowlisted_release_requires_approved_release_time_quality(tmp_path):
    db = AnalysisDatabase(tmp_path / "m251b-release-time-gate.db")
    base = dict(
        source_system="fred",
        release_name="US CPI",
        country="US",
        event_type="macro_data_release",
        scheduled_at_utc="2026-07-14T12:30:00+00:00",
        released_at_utc="2026-07-14T12:30:00+00:00",
        available_at_utc="2026-07-14T12:30:05+00:00",
        model_visible_at_utc="2026-07-14T12:30:05+00:00",
    )
    with db.connect() as conn:
        observation_only_id = upsert_macro_event_release(
            conn,
            MacroEventReleaseInput(
                **base,
                release_time_quality="observation_date_only",
                observations=(
                    MacroObservationInput(series_id="CPIAUCSL", period="2026-06", data_version="observation"),
                ),
            ),
        )
        exact_id = upsert_macro_event_release(
            conn,
            MacroEventReleaseInput(
                **{
                    **base,
                    "scheduled_at_utc": "2026-08-12T12:30:00+00:00",
                    "released_at_utc": "2026-08-12T12:30:00+00:00",
                    "available_at_utc": "2026-08-12T12:30:05+00:00",
                    "model_visible_at_utc": "2026-08-12T12:30:05+00:00",
                },
                release_time_quality="official_exact",
                observations=(
                    MacroObservationInput(series_id="CPIAUCSL", period="2026-07", data_version="official"),
                ),
            ),
        )
        observation_only = conn.execute(
            "SELECT * FROM macro_event_releases WHERE macro_event_id=?", (observation_only_id,)
        ).fetchone()
        exact = conn.execute("SELECT * FROM macro_event_releases WHERE macro_event_id=?", (exact_id,)).fetchone()

    assert observation_only["reaction_trigger_eligible"] == 0
    assert observation_only["intraday_label_eligible"] == 0
    assert exact["reaction_trigger_eligible"] == 1
    assert exact["intraday_label_eligible"] == 1


def test_global_central_bank_seed_covers_fed_ecb_boj(tmp_path):
    db_path = tmp_path / "m251b-global-central-bank-seed.db"
    AnalysisDatabase(db_path)
    rebuild_macro_event_lineage_from_observations(
        str(db_path),
        source="central_bank",
        start="2025-01-01",
        end="2025-03-31",
    )
    with AnalysisDatabase(db_path).connect() as conn:
        banks = {
            str(row["central_bank"])
            for row in conn.execute("SELECT DISTINCT central_bank FROM central_bank_events").fetchall()
        }
        bad_intraday = conn.execute(
            """SELECT COUNT(*) AS c FROM macro_event_releases
                WHERE intraday_label_eligible=1
                  AND release_time_quality NOT IN ('official_exact','provider_exact')"""
        ).fetchone()["c"]

    assert banks == {"Fed", "ECB", "BOJ"}
    assert bad_intraday == 0
