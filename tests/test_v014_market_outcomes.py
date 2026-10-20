from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from recorder.analysis_database import AnalysisDatabase
from recorder.market_outcomes import (
    DailyBar,
    IBKRHistoricalDailyPriceSource,
    IBKRHistoricalIntradayPriceSource,
    IntradayBar,
    ReplayMarketOutcomeBuilder,
    _IBKRHistoricalClient,
    classify_ibkr_historical_error,
    _ibkr_errors_indicate_unknown_contract,
    primary_exchange_candidates_for_symbol,
)
from recorder.replay_corpus import ReplayCorpusBuilder


class FixtureDailyPriceSource:
    name = "fixture_daily_close"

    def __init__(self, bars: dict[str, list[DailyBar]]) -> None:
        self.bars = {symbol.upper(): values for symbol, values in bars.items()}

    def get_daily_bars(self, symbol: str, start, end) -> list[DailyBar]:
        return [
            bar for bar in self.bars.get(symbol.upper(), [])
            if start <= bar.date <= end
        ]


class FixtureIntradayPriceSource:
    name = "fixture_intraday_5_mins_all_sessions"

    def __init__(self, bars: dict[str, list[IntradayBar]]) -> None:
        self.bars = {symbol.upper(): values for symbol, values in bars.items()}

    def get_intraday_bars(self, symbol: str, start, end) -> list[IntradayBar]:
        return [
            bar for bar in self.bars.get(symbol.upper(), [])
            if start <= bar.timestamp <= end
        ]


def _bar(symbol: str, value: str, close: float) -> DailyBar:
    from datetime import datetime

    return DailyBar(symbol=symbol, date=datetime.strptime(value, "%Y-%m-%d").date(), close=close)


def _ibar(symbol: str, value: str, close: float) -> IntradayBar:
    from datetime import datetime, timezone

    return IntradayBar(
        symbol=symbol,
        timestamp=datetime.fromisoformat(value).astimezone(timezone.utc),
        close=close,
    )


def _official_doc(**overrides):
    values = {
        "source_type": "sec",
        "source_url": "https://www.sec.gov/Archives/example/8k.htm",
        "title": "8-K Example",
        "content": "Official financial results release.",
        "content_hash": "abc123",
        "document_type": "8-K",
        "event_type": "earnings",
        "actual_subject_symbol": "MU",
        "published_at": "2026-06-24T20:02:01+00:00",
        "accepted_at": "2026-06-24T20:02:01+00:00",
        "first_seen_at": "2026-06-24T20:02:01+00:00",
        "available_at": "2026-06-24T20:02:01+00:00",
        "external_document_id": "sec:test-accession",
        "raw_snapshot": "<html>Official financial results release.</html>",
        "materiality_classification": "material_earnings",
        "materiality_reason": "test",
        "ingestion_version": "test",
        "status": "fetched",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _historical_event(document_id: int, **overrides):
    values = {
        "event_type": "earnings",
        "event_subtype": "quarterly_results",
        "actual_subject_symbol": "MU",
        "official_document_id": document_id,
        "event_timestamp": "2026-06-24T20:02:01+00:00",
        "available_at": "2026-06-24T20:02:01+00:00",
        "source_hash": "abc123",
        "prediction_cutoff": "2026-06-24T20:02:01+00:00",
        "market_session": "after_hours",
        "replay_eligibility": "eligible",
        "replay_eligibility_reasons": (),
        "ingestion_version": "test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _build_corpus(db: AnalysisDatabase, event) -> None:
    db.upsert_historical_event(event)
    result = ReplayCorpusBuilder(db).build(event_type=event.event_type)
    assert result["eligible"] == 1


def test_replay_outcome_after_hours_uses_same_day_close_then_next_trading_close(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-after-hours.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    _build_corpus(db, _historical_event(document_id))
    source = FixtureDailyPriceSource(
        {
            "MU": [_bar("MU", "2026-06-24", 100.0), _bar("MU", "2026-06-25", 110.0)],
            "SPY": [_bar("SPY", "2026-06-24", 500.0), _bar("SPY", "2026-06-25", 505.0)],
        }
    )

    first = ReplayMarketOutcomeBuilder(db, source).build(horizons=("1D",), benchmark_symbol="SPY")
    second = ReplayMarketOutcomeBuilder(db, source).build(horizons=("1D",), benchmark_symbol="SPY")

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 1
    rows = db.recent_replay_market_outcomes(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["baseline_at"] == "2026-06-24"
    assert row["reaction_at"] == "2026-06-25"
    assert row["price_status"] == "complete"
    assert round(float(row["raw_return"]), 6) == 0.1
    assert round(float(row["benchmark_return"]), 6) == 0.01
    assert round(float(row["excess_return"]), 6) == 0.09
    assert row["direction_label"] == "up"


def test_replay_outcome_pre_market_uses_previous_close_then_event_day_close(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-pre-market.db")
    document_id, _ = db.upsert_official_document(
        _official_doc(
            external_document_id="sec:premarket",
            published_at="2026-06-25T12:02:01+00:00",
            accepted_at="2026-06-25T12:02:01+00:00",
            first_seen_at="2026-06-25T12:02:01+00:00",
            available_at="2026-06-25T12:02:01+00:00",
        )
    )
    _build_corpus(
        db,
        _historical_event(
            document_id,
            event_timestamp="2026-06-25T12:02:01+00:00",
            available_at="2026-06-25T12:02:01+00:00",
            prediction_cutoff="2026-06-25T12:02:01+00:00",
            market_session="pre_market",
        ),
    )
    source = FixtureDailyPriceSource(
        {
            "MU": [_bar("MU", "2026-06-24", 100.0), _bar("MU", "2026-06-25", 96.0)],
            "SPY": [_bar("SPY", "2026-06-24", 500.0), _bar("SPY", "2026-06-25", 510.0)],
        }
    )

    result = ReplayMarketOutcomeBuilder(db, source).build(horizons=("1D",), benchmark_symbol="SPY")

    assert result["completed"] == 1
    row = db.recent_replay_market_outcomes(limit=1)[0]
    assert row["baseline_at"] == "2026-06-24"
    assert row["reaction_at"] == "2026-06-25"
    assert round(float(row["raw_return"]), 6) == -0.04
    assert row["direction_label"] == "down"


def test_replay_outcome_does_not_mutate_replay_input_snapshot(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-snapshot.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    _build_corpus(db, _historical_event(document_id))
    before = db.recent_replay_corpus(limit=1)[0]["input_snapshot_json"]
    source = FixtureDailyPriceSource(
        {
            "MU": [_bar("MU", "2026-06-24", 100.0), _bar("MU", "2026-06-25", 101.0)],
            "SPY": [_bar("SPY", "2026-06-24", 500.0), _bar("SPY", "2026-06-25", 501.0)],
        }
    )

    ReplayMarketOutcomeBuilder(db, source).build(horizons=("1D",), benchmark_symbol="SPY")

    after = db.recent_replay_corpus(limit=1)[0]["input_snapshot_json"]
    assert after == before
    snapshot = json.loads(after)
    serialized = json.dumps(snapshot)
    assert "raw_return" not in serialized
    assert "reaction_price" not in serialized
    assert "prediction_outcome" not in serialized


def test_replay_outcome_skips_database_writes_when_price_source_returns_no_data(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-no-data.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    _build_corpus(db, _historical_event(document_id))
    source = FixtureDailyPriceSource({})

    result = ReplayMarketOutcomeBuilder(db, source).build(horizons=("1D",), benchmark_symbol="SPY")

    assert result["source_data_available"] is False
    assert result["blocked"] == 1
    assert db.recent_replay_market_outcomes(limit=10) == []


def test_replay_outcome_keeps_price_sources_separate(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-source-boundary.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    _build_corpus(db, _historical_event(document_id))
    bars = {
        "MU": [_bar("MU", "2026-06-24", 100.0), _bar("MU", "2026-06-25", 101.0)],
        "SPY": [_bar("SPY", "2026-06-24", 500.0), _bar("SPY", "2026-06-25", 501.0)],
    }
    source_a = FixtureDailyPriceSource(bars)
    source_a.name = "fixture_a"
    source_b = FixtureDailyPriceSource(bars)
    source_b.name = "fixture_b"

    first = ReplayMarketOutcomeBuilder(db, source_a).build(horizons=("1D",), benchmark_symbol="SPY")
    second = ReplayMarketOutcomeBuilder(db, source_b).build(horizons=("1D",), benchmark_symbol="SPY")

    assert first["created"] == 1
    assert second["created"] == 1
    rows = db.recent_replay_market_outcomes(limit=10)
    assert {row["price_source"] for row in rows} == {"fixture_a", "fixture_b"}
    filtered = db.recent_replay_market_outcomes(limit=10, price_source="fixture_a")
    assert len(filtered) == 1
    assert filtered[0]["price_source"] == "fixture_a"


def test_intraday_outcome_uses_pre_event_bar_and_post_horizon_bar(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-intraday.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    _build_corpus(db, _historical_event(document_id))
    source = FixtureIntradayPriceSource(
        {
            "MU": [
                _ibar("MU", "2026-06-24T20:00:00+00:00", 100.0),
                _ibar("MU", "2026-06-24T20:05:00+00:00", 102.0),
                _ibar("MU", "2026-06-24T20:30:00+00:00", 110.0),
                _ibar("MU", "2026-06-24T20:35:00+00:00", 112.0),
                _ibar("MU", "2026-06-24T22:05:00+00:00", 120.0),
            ],
            "SPY": [
                _ibar("SPY", "2026-06-24T20:00:00+00:00", 500.0),
                _ibar("SPY", "2026-06-24T20:30:00+00:00", 505.0),
                _ibar("SPY", "2026-06-24T20:35:00+00:00", 506.0),
                _ibar("SPY", "2026-06-24T22:05:00+00:00", 510.0),
            ],
        }
    )

    result = ReplayMarketOutcomeBuilder(db, source).build(horizons=("30M", "2H"), benchmark_symbol="SPY")

    assert result["completed"] == 2
    rows = sorted(db.recent_replay_market_outcomes(limit=10), key=lambda row: row["horizon"])
    by_horizon = {row["horizon"]: row for row in rows}
    assert by_horizon["30M"]["baseline_at"] == "2026-06-24T20:00:00+00:00"
    assert by_horizon["30M"]["reaction_at"] == "2026-06-24T20:35:00+00:00"
    assert round(float(by_horizon["30M"]["raw_return"]), 6) == 0.12
    assert by_horizon["2H"]["reaction_at"] == "2026-06-24T22:05:00+00:00"
    assert round(float(by_horizon["2H"]["raw_return"]), 6) == 0.2


def test_intraday_horizon_with_daily_source_is_not_written(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "outcomes-intraday-daily-source.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    _build_corpus(db, _historical_event(document_id))
    source = FixtureDailyPriceSource(
        {
            "MU": [_bar("MU", "2026-06-24", 100.0), _bar("MU", "2026-06-25", 101.0)],
        }
    )

    result = ReplayMarketOutcomeBuilder(db, source).build(horizons=("30M",), benchmark_symbol="")

    assert result["source_data_available"] is False
    assert result["source_blocker_reason"] == "price_source_does_not_support_intraday_bars"
    assert db.recent_replay_market_outcomes(limit=10) == []


def test_ibkr_historical_price_source_is_lazy_and_named() -> None:
    source = IBKRHistoricalDailyPriceSource(
        host="127.0.0.1",
        port=4001,
        client_id=3021,
        timeout_seconds=0.01,
        what_to_show="TRADES",
    )

    assert source.name == "ibkr_historical_trades_daily"
    assert source._client is None


def test_ibkr_intraday_price_source_is_lazy_and_named() -> None:
    source = IBKRHistoricalIntradayPriceSource(
        host="127.0.0.1",
        port=4001,
        client_id=4021,
        timeout_seconds=0.01,
        what_to_show="TRADES",
        bar_size="5 mins",
        use_rth=False,
    )

    assert source.name == "ibkr_historical_trades_5_mins_all_sessions"
    assert source._client is None


def test_ibkr_stock_contract_adds_primary_exchange_hints_for_core_symbols() -> None:
    qqq = _IBKRHistoricalClient.stock_contract("QQQ")
    spy = _IBKRHistoricalClient.stock_contract("SPY")
    nvda = _IBKRHistoricalClient.stock_contract("NVDA")
    tsm = _IBKRHistoricalClient.stock_contract("TSM")
    ita = _IBKRHistoricalClient.stock_contract("ITA")
    xar = _IBKRHistoricalClient.stock_contract("XAR")
    unknown = _IBKRHistoricalClient.stock_contract("NOTREAL")

    assert qqq.exchange == "SMART"
    assert qqq.primaryExchange == "NASDAQ"
    assert spy.primaryExchange == "ARCA"
    assert nvda.primaryExchange == "NASDAQ"
    assert tsm.primaryExchange == "NYSE"
    assert ita.primaryExchange == "BATS"
    assert xar.primaryExchange == "ARCA"
    assert not getattr(unknown, "primaryExchange", "")


def test_ibkr_unknown_contract_error_detection() -> None:
    assert _ibkr_errors_indicate_unknown_contract(
        "162 Historical Market Data Service error message:Unknown contract"
    )
    assert not _ibkr_errors_indicate_unknown_contract("2106 HMDS data farm connection is OK")


def test_unknown_symbol_gets_bounded_primary_exchange_fallback_candidates() -> None:
    assert primary_exchange_candidates_for_symbol("QQQ")[:2] == ("NASDAQ", "ISLAND")
    assert primary_exchange_candidates_for_symbol("NEWETF") == ("NASDAQ", "ISLAND", "ARCA", "NYSE", "BATS", "AMEX")


def test_ibkr_resolve_unknown_symbol_can_try_fallback_primary_exchanges() -> None:
    class FakeHistoricalClient(_IBKRHistoricalClient):
        def __init__(self) -> None:
            super().__init__()
            self.primary_exchange_calls: list[str] = []

        def _resolve_stock_contract_once(
            self,
            symbol: str,
            timeout_seconds: float,
            *,
            use_primary_exchange_hint: bool,
            require_primary_exchange: bool,
            primary_exchange: str | None = None,
        ):
            self.primary_exchange_calls.append(str(primary_exchange or ""))
            if primary_exchange == "ARCA":
                return self.stock_contract(symbol, primary_exchange=primary_exchange), []
            return None, ["162 Historical Market Data Service error message:Unknown contract"]

    client = FakeHistoricalClient()

    contract = client.resolve_stock_contract("NEWETF", 0.01, refresh=True, require_primary_exchange=True)

    assert contract.primaryExchange == "ARCA"
    assert client.primary_exchange_calls == ["NASDAQ", "ISLAND", "ARCA"]


def test_ibkr_historical_error_classification_distinguishes_permission_and_xom_contract_change() -> None:
    permission = classify_ibkr_historical_error(
        "162 Historical Market Data Service error message:No market data permissions for ARCA STK",
        symbol="GLD",
    )
    assert permission.category == "permission_missing"
    assert permission.retryable is False

    xom = classify_ibkr_historical_error(
        "162 Historical Market Data Service error message:Unknown contract",
        symbol="XOM",
        start=__import__("datetime").date(2025, 1, 2),
    )
    assert xom.category == "corporate_action_or_new_conid"
    assert xom.retryable is False


def test_ibkr_intraday_unknown_contract_retries_with_primary_exchange_hint() -> None:
    from datetime import datetime, timezone

    class FakeHistoricalClient(_IBKRHistoricalClient):
        def __init__(self) -> None:
            super().__init__()
            self.resolve_calls: list[tuple[bool, bool]] = []
            self.historical_primary_exchanges: list[str] = []

        def resolve_stock_contract(
            self,
            symbol: str,
            timeout_seconds: float,
            *,
            refresh: bool = False,
            require_primary_exchange: bool = False,
        ):
            self.resolve_calls.append((refresh, require_primary_exchange))
            contract = self.stock_contract(symbol, use_primary_exchange_hint=require_primary_exchange)
            if not require_primary_exchange:
                contract.primaryExchange = ""
            return contract

        def reqHistoricalData(self, reqId, contract, *args):  # noqa: N802
            self.historical_primary_exchanges.append(str(getattr(contract, "primaryExchange", "") or ""))
            if len(self.historical_primary_exchanges) == 1:
                self._errors.setdefault(int(reqId), []).append(
                    "162 Historical Market Data Service error message:Unknown contract"
                )
                self._events[int(reqId)].set()
                return
            self._intraday_bars.setdefault(int(reqId), []).append(
                IntradayBar(
                    symbol="QQQ",
                    timestamp=datetime(2026, 6, 24, 14, 31, tzinfo=timezone.utc),
                    close=100.0,
                )
            )
            self._events[int(reqId)].set()

    client = FakeHistoricalClient()

    rows = client.request_intraday_bars(
        symbol="QQQ",
        start=datetime(2026, 6, 24, 14, 30, tzinfo=timezone.utc),
        end=datetime(2026, 6, 24, 14, 35, tzinfo=timezone.utc),
        timeout_seconds=0.01,
        bar_size="1 min",
        what_to_show="TRADES",
        use_rth=False,
    )

    assert len(rows) == 1
    assert rows[0].close == 100.0
    assert client.resolve_calls == [(False, False), (True, True)]
    assert client.historical_primary_exchanges == ["", "NASDAQ"]
