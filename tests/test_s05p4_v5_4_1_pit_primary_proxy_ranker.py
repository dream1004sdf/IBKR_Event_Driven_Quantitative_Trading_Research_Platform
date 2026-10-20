from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from recorder.s05_proxy_semantic_v5 import V5PolicyConfig
from scripts.run_s05p4_v5_4_1_pit_primary_proxy_ranker import (
    OUTPUT_TABLES,
    build_ranker_rows,
    feature_metrics_for_symbol,
    persist_payload,
    table_state,
)


FEATURE_POLICY = {
    "lookback_completed_sessions": 20,
    "minimum_completed_sessions": 10,
    "maximum_last_session_age_calendar_days": 7,
    "dollar_volume_floor": 100000.0,
    "dollar_volume_ceiling": 10000000000.0,
    "daily_trade_count_floor": 100.0,
    "daily_trade_count_ceiling": 1000000.0,
    "daily_high_low_spread_ceiling": 0.04,
    "family_exposure_neutral_prior": 0.5,
    "beta_purity_neutral_prior": 0.5,
}


def make_cache() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE ibkr_historical_bars (
            symbol TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            bar_size TEXT NOT NULL,
            what_to_show TEXT NOT NULL,
            use_rth INTEGER NOT NULL,
            bar_time_utc TEXT NOT NULL,
            session_date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            trade_count INTEGER
        )
        """
    )
    return connection


def insert_daily(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    session_date: str,
    close: float = 100.0,
    volume: float = 1_000_000.0,
    trade_count: int = 10_000,
) -> None:
    connection.execute(
        """
        INSERT INTO ibkr_historical_bars VALUES
        (?, 'daily', '1 day', 'TRADES', 1, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            symbol,
            f"{session_date}T00:00:00+00:00",
            session_date,
            close - 0.5,
            close + 1.0,
            close - 1.0,
            close,
            volume,
            trade_count,
        ),
    )


def populate_prior_rows(connection: sqlite3.Connection, symbol: str) -> None:
    start = datetime(2025, 1, 2, tzinfo=UTC)
    for offset in range(14):
        day = start + timedelta(days=offset)
        insert_daily(
            connection,
            symbol=symbol,
            session_date=day.date().isoformat(),
            close=100.0 + offset,
            volume=1_000_000.0 + 1000 * offset,
            trade_count=10_000 + offset,
        )


def ranker_config() -> dict[str, object]:
    return {
        "feature_policy": FEATURE_POLICY,
        "adjudication_policy": {
            "eligible_family_resolution_prefix": "resolved_",
            "resolved_family_confidence": 1.0,
        },
        "selection_policy": {
            "enforce_v5_primary_proxy_confidence_threshold": True,
        },
    }


def test_daily_feature_cutoff_excludes_same_event_session_bar() -> None:
    connection = make_cache()
    populate_prior_rows(connection, "IEF")
    insert_daily(
        connection,
        symbol="IEF",
        session_date="2025-01-20",
        close=9999.0,
        volume=999_999_999.0,
        trade_count=9_999_999,
    )
    result = feature_metrics_for_symbol(
        connection,
        symbol="IEF",
        event_time=datetime(2025, 1, 20, 15, 0, tzinfo=UTC),
        feature_policy=FEATURE_POLICY,
    )
    assert result["complete"] is True
    assert result["max_source_session_date"] < result["event_session_date"]
    assert result["median_dollar_volume"] < 1_000_000_000.0
    assert set(result["metrics"]) == {
        "median_dollar_volume_score",
        "spread_quality",
        "intraday_trade_coverage",
        "stale_trade_quality",
        "market_session_overlap",
        "family_exposure",
        "beta_purity",
        "data_availability",
    }


def test_missing_family_candidate_forces_abstention() -> None:
    connection = make_cache()
    populate_prior_rows(connection, "ITA")
    payload = build_ranker_rows(
        safe_input_rows=[{
            "review_row_id": "row-1",
            "normalized_headline": "A direct conflict event",
            "plan_as_of_utc": "2025-01-20T15:00:00+00:00",
            "provider": "bz",
        }],
        semantic_rows={
            "row-1": {
                "event_cluster_id": 1,
                "direction": "escalation",
                "affected_channel": "defense_demand",
                "proxy_family": "defense_conflict",
                "family_resolution_status": "resolved_corrected",
                "primary_channel": "defense_demand",
            }
        },
        cache_connection=connection,
        ranker_config=ranker_config(),
        semantic_policy=V5PolicyConfig(),
    )
    decision = payload["decisions"][0]
    assert decision["primary_proxy_selected"] == ""
    assert decision["abstained"] == 1
    assert decision["all_candidates_complete"] == 0
    assert decision["final_abstain_reason"] == "incomplete_candidate_market_data:XAR"
    assert payload["snapshots"][0]["pit_violation_count"] == 0


def test_same_payload_persistence_is_idempotent() -> None:
    cache = make_cache()
    populate_prior_rows(cache, "ITA")
    payload = build_ranker_rows(
        safe_input_rows=[{
            "review_row_id": "row-1",
            "normalized_headline": "A direct conflict event",
            "plan_as_of_utc": "2025-01-20T15:00:00+00:00",
            "provider": "bz",
        }],
        semantic_rows={
            "row-1": {
                "event_cluster_id": 1,
                "direction": "escalation",
                "affected_channel": "defense_demand",
                "proxy_family": "defense_conflict",
                "family_resolution_status": "resolved_corrected",
                "primary_channel": "defense_demand",
            }
        },
        cache_connection=cache,
        ranker_config=ranker_config(),
        semantic_policy=V5PolicyConfig(),
    )
    output = sqlite3.connect(":memory:")
    run = {
        "run_key": "run-1",
        "version": "test",
        "config_hash": "config",
        "input_hash": "input",
        "source_clone_sha256": "source",
        "cache_sha256": "cache",
        "canonical_sha256": "canonical",
        "set_a_rows": 1,
        "created_at_utc": "2025-01-01T00:00:00+00:00",
    }
    persist_payload(output, run_row=run, payload=payload)
    first = table_state(output)
    persist_payload(output, run_row=run, payload=payload)
    second = table_state(output)
    assert first == second
    assert all(first[table]["count"] >= 1 for table in OUTPUT_TABLES)
