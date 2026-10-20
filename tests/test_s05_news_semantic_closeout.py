import sqlite3
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from recorder.ibkr_broadtape_news import _is_informational_ibkr_code
from recorder.s04_market_data_pre_s05_audit import calendar_schedule
from recorder.s05_news_semantic_closeout import (
    REQUIRED_CORE_ASSETS,
    assign_event_series,
    calendar_semantics,
    classify_headline,
    explicit_tickers,
    normalize_headline,
    resolve_entity_roles,
)
from scripts.run_s05p1_news_label_pilot import classify_no_trade_shard, select_assets


def test_normalization_removes_ibkr_prefix_and_decodes_html() -> None:
    raw = "{A:800015:L:en:K:0.1:C:0.9}Trump&#x2014;Market Update"
    assert normalize_headline(raw) == "Trump—Market Update"


def test_analyst_action_uses_subject_not_analyst_as_direct() -> None:
    headline = "UBS Reduces Its Price Objective For Duke Energy Corporation (DUK)"
    semantic = classify_headline(headline)
    roles = resolve_entity_roles(
        headline,
        semantic,
        [
            {"symbol": "DUK", "resolver_status": "unresolved", "tradable_candidate": 0, "source": "marketaux_entity"},
            {"symbol": "MS", "resolver_status": "resolved", "tradable_candidate": 1, "source": "content_entity"},
        ],
    )
    assert semantic.news_content_type == "analyst_action"
    assert explicit_tickers(headline) == ["DUK"]
    assert next(row for row in roles if row["symbol"] == "DUK")["entity_role"] == "issuer"
    assert next(row for row in roles if row["symbol"] == "DUK")["direct_eligible"] == 1
    source = next(row for row in roles if row["entity_role"] == "analyst_or_source")
    assert source["symbol"] == "SOURCE:UBS"
    assert source["direct_eligible"] == 0
    assert all(row["symbol"] != "MS" for row in roles)


def test_context_content_is_not_causal() -> None:
    for headline in (
        "A Look Into Enphase Energy's Price Over Earnings (ENPH)",
        "Stock Market Today: Live Coverage",
        "Chevron shares are trading higher after oil gains",
    ):
        assert not classify_headline(headline).causal_trigger_eligible


def test_holiday_is_market_closed() -> None:
    schedule = calendar_schedule(date(2025, 12, 20), date(2026, 1, 10))
    result = calendar_semantics(datetime(2026, 1, 1, 15, 0, tzinfo=UTC), schedule)
    assert result["is_trading_day"] == 0
    assert result["session_bucket"] == "market_closed"
    assert result["first_regular_session_date"] == "2026-01-02"


def test_duplicate_series_share_one_effective_sample() -> None:
    rows = [
        {"normalized_headline": "Dow Jones Futures Fall Ahead Of Fed", "plan_as_of_utc": "2025-01-01T00:00:00+00:00", "event_cluster_id": 1},
        {"normalized_headline": "Update: Dow Jones Futures Fall Ahead Of Fed", "plan_as_of_utc": "2025-01-01T00:01:00+00:00", "event_cluster_id": 2},
    ]
    assign_event_series(rows)
    assert rows[0]["event_series_id"] == rows[1]["event_series_id"]
    assert sum(row["series_sample_weight"] for row in rows) == 1.0


def test_ibkr_connecting_code_is_informational() -> None:
    assert _is_informational_ibkr_code(2119)


def test_required_core_is_attached_and_context_is_not_direct() -> None:
    selection = {
        "clusters": [
            {"event_cluster_id": 1, "causal_trigger_eligible": 0, "direct_roles": [{"symbol": "MSFT"}]},
            {"event_cluster_id": 2, "causal_trigger_eligible": 1, "direct_roles": [{"symbol": "DUK"}]},
        ]
    }
    assets = select_assets(selection)
    required = {symbol for symbol, _, _ in REQUIRED_CORE_ASSETS}
    assert {row["symbol"] for row in assets[1]} == required
    assert all(row["asset_role"] != "direct_entity" for row in assets[1])
    assert required.issubset({row["symbol"] for row in assets[2]})
    assert next(row for row in assets[2] if row["symbol"] == "DUK")["asset_role"] == "direct_entity"


def test_hmds_no_data_is_only_classified_with_benchmark_quorum() -> None:
    start = datetime(2025, 1, 15, 10, 4, 42, tzinfo=UTC)
    end = start + timedelta(hours=2, minutes=15)
    shard = SimpleNamespace(
        shard_key="uup-no-trade",
        symbol="UUP",
        data_kind="intraday",
        start_at_utc=start,
        end_at_utc=end,
    )
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            """CREATE TABLE ibkr_historical_bars(
                   symbol TEXT,data_kind TEXT,what_to_show TEXT,bar_time_utc TEXT
               )"""
        )
        for symbol in ("SPY", "QQQ", "IWM"):
            conn.execute(
                "INSERT INTO ibkr_historical_bars VALUES(?,?,?,?)",
                (symbol, "intraday", "TRADES", (start + timedelta(minutes=1)).isoformat()),
            )
        result = classify_no_trade_shard(conn, shard, "162 HMDS query returned no data")
        assert result["classification"] == "no_trade_or_symbol_specific_candidate"
        assert result["benchmark_observed_count"] == 3
        assert result["midpoint_substituted"] is False


def test_hmds_no_data_without_quorum_stays_unclassified() -> None:
    start = datetime(2025, 1, 15, 10, 4, 42, tzinfo=UTC)
    shard = SimpleNamespace(
        shard_key="provider-gap",
        symbol="UUP",
        data_kind="intraday",
        start_at_utc=start,
        end_at_utc=start + timedelta(hours=2),
    )
    with sqlite3.connect(":memory:") as conn:
        conn.execute(
            """CREATE TABLE ibkr_historical_bars(
                   symbol TEXT,data_kind TEXT,what_to_show TEXT,bar_time_utc TEXT
               )"""
        )
        result = classify_no_trade_shard(conn, shard, "162 HMDS query returned no data")
        assert result["classification"] == "unclassified_hmds_no_data"
