import json
from datetime import datetime, timezone

from ibapi.contract import Contract

from recorder.config import Settings
from recorder.ibkr_broadtape_news import IBKRBroadTapeNewsProbe
from recorder.ibkr_app import IBKRMarketEventRecorder
from recorder.models import QuoteSnapshot

UTC = timezone.utc


def test_tick_news_end_to_end_without_live_socket(tmp_path):
    settings = Settings(
        watch_symbols=("MU",),
        benchmark_symbols=("SPY",),
        database_path=tmp_path / "pipeline.db",
        fetch_article_body=False,
        reaction_horizons=("1m",),
        price_max_age_seconds=600,
    )
    app = IBKRMarketEventRecorder(settings)
    app._news_req_to_symbol[5001] = "MU"
    contract = Contract()
    contract.symbol = "MU"
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.conId = 12345
    app._contracts["MU"] = contract

    now = datetime.now(UTC)
    app.prices.put_snapshot(
        QuoteSnapshot("MU", now, bid=99.9, ask=100.1, last=100.0)
    )
    app.prices.put_snapshot(
        QuoteSnapshot("SPY", now, bid=599.9, ask=600.1, last=600.0)
    )

    app.tickNews(
        5001,
        int(now.timestamp()),
        "DJ-RT",
        "PIPE-1",
        "Micron raises quarterly revenue outlook on stronger AI memory demand",
        "",
    )
    summary = app.db.summary()
    assert summary["news"] == 1
    assert summary["baselines"] == 2
    assert summary["pending_targets"] == 2

    app.tickNews(
        5001,
        int(now.timestamp()),
        "DJ-RT",
        "PIPE-1",
        "Micron raises quarterly revenue outlook on stronger AI memory demand",
        "",
    )
    assert app.db.summary()["duplicates"] == 1


def test_recorder_subscribes_news_for_geopolitical_proxy_symbols(tmp_path):
    settings = Settings(
        watch_symbols=("MU",),
        benchmark_symbols=("SPY",),
        geopolitical_news_symbols=("TLT", "SOXX", "UUP"),
        database_path=tmp_path / "geo-news.db",
        fetch_article_body=False,
    )
    app = IBKRMarketEventRecorder(settings)
    app._providers = {"DJ-RT": "Dow Jones Trader News"}
    app._providers_received = True

    for request_id, symbol in enumerate(settings.all_price_symbols, start=1001):
        app._contract_req_to_symbol[request_id] = symbol
        app._contract_requests_completed.add(request_id)
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.conId = request_id
        app._contracts[symbol] = contract

    subscribed: list[tuple[str, str]] = []

    def fake_req_mkt_data(_request_id, contract, generic_ticks, _snapshot, _regulatory_snapshot, _options):
        if "292:" in str(generic_ticks):
            subscribed.append((contract.symbol, str(generic_ticks)))

    app.reqMktData = fake_req_mkt_data  # type: ignore[method-assign]
    app._try_start_subscriptions()

    assert {symbol for symbol, _ in subscribed} == {"MU", "SPY", "TLT", "SOXX", "UUP"}
    assert all("292:DJ-RT" in generic_ticks for _, generic_ticks in subscribed)


def test_recorder_can_add_benzinga_broadtape_subscription_without_removing_ticker_news(tmp_path):
    settings = Settings(
        watch_symbols=("NVDA",),
        benchmark_symbols=("SPY",),
        geopolitical_news_symbols=(),
        database_path=tmp_path / "broadtape-enabled.db",
        fetch_article_body=False,
        news_provider_codes=("BZ",),
        ibkr_broadtape_enabled=True,
        ibkr_broadtape_provider_codes=("BZ",),
    )
    app = IBKRMarketEventRecorder(settings)
    app._providers = {"BZ": "Benzinga"}
    app._providers_received = True

    for request_id, symbol in enumerate(settings.all_price_symbols, start=1001):
        app._contract_req_to_symbol[request_id] = symbol
        app._contract_requests_completed.add(request_id)
        contract = Contract()
        contract.symbol = symbol
        contract.secType = "STK"
        contract.exchange = "SMART"
        contract.currency = "USD"
        contract.conId = request_id
        app._contracts[symbol] = contract

    ticker_news: list[tuple[str, str]] = []
    broadtape_news: list[tuple[str, str, str]] = []

    def fake_req_mkt_data(_request_id, contract, generic_ticks, _snapshot, _regulatory_snapshot, _options):
        if "292" not in str(generic_ticks):
            return
        if contract.secType == "NEWS":
            broadtape_news.append((contract.symbol, contract.exchange, str(generic_ticks)))
        else:
            ticker_news.append((contract.symbol, str(generic_ticks)))

    app.reqMktData = fake_req_mkt_data  # type: ignore[method-assign]
    app._try_start_subscriptions()

    assert {symbol for symbol, _ in ticker_news} == {"NVDA", "SPY"}
    assert broadtape_news == [("BZ:BZ_ALL", "BZ", "mdoff,292")]


def test_broadtape_tick_news_can_be_stored_as_separate_primary_symbol(tmp_path):
    settings = Settings(
        watch_symbols=("NVDA",),
        benchmark_symbols=("SPY",),
        database_path=tmp_path / "broadtape-probe.db",
        fetch_article_body=False,
    )
    probe = IBKRBroadTapeNewsProbe(
        settings,
        provider_codes=("BZ",),
        seconds=1,
        save_to_db=True,
    )

    now = datetime.now(UTC)
    probe.tickNews(
        860001,
        int(now.timestamp()),
        "BZ",
        "BT-1",
        "Breaking: BroadTape headline crosses without a ticker-specific subscription",
        "",
    )

    summary = probe.db.summary()
    assert summary["news"] == 1
    row = probe.db.get_news_by_identity("BZ", "BT-1")
    assert row["primary_symbol"] == "BROADTAPE:BZ"
    extra = json.loads(row["extra_data"])
    assert extra["source_mode"] == "ibkr_broadtape"
    assert extra["route_type"] == "broadtape"
    assert extra["asset_mapping_deferred"] is True


def test_tick_news_emergency_headline_creates_breaking_overlay_signal(tmp_path):
    settings = Settings(
        watch_symbols=("NVDA",),
        benchmark_symbols=("SPY",),
        database_path=tmp_path / "breaking-pipeline.db",
        fetch_article_body=False,
        reaction_horizons=("1m",),
    )
    app = IBKRMarketEventRecorder(settings)
    app._news_req_to_symbol[5002] = "NVDA"
    contract = Contract()
    contract.symbol = "NVDA"
    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"
    contract.conId = 7788
    app._contracts["NVDA"] = contract

    now = datetime.now(UTC)
    app.tickNews(
        5002,
        int(now.timestamp()),
        "BZ",
        "BREAKING-1",
        "Breaking: Taiwan blockade threat hits advanced semiconductor supply chain",
        "",
    )

    rows = app.db.recent_external_market_signals(source="emergency_breaking_news", limit=5)
    assert len(rows) == 1
    assert rows[0]["source"] == "emergency_breaking_news"
    assert rows[0]["prediction_gate_status"] == "eligible"
    assert rows[0]["region"] == "taiwan_china"
    assert "NVDA" in json.loads(rows[0]["assets_json"])
    raw = json.loads(rows[0]["raw_json"])
    assert raw["system_action"] == "immediate_scorer_alert"
    assert raw["news_article_id"] == 1


def test_recorder_connection_closed_schedules_reconnect_and_writes_status(tmp_path):
    status_path = tmp_path / "ibkr_connection_status.json"
    settings = Settings(
        watch_symbols=("NVDA",),
        benchmark_symbols=("SPY",),
        database_path=tmp_path / "reconnect.db",
        fetch_article_body=False,
        ibkr_connection_status_json_path=status_path,
        ibkr_reconnect_enabled=True,
    )
    app = IBKRMarketEventRecorder(settings)

    app.connectionClosed()

    payload = json.loads(status_path.read_text(encoding="utf-8"))
    assert payload["status"] == "disconnected"
    assert payload["last_event"] == "connection_closed"
    assert payload["reconnect_enabled"] is True
    assert app._next_reconnect_at_monotonic > 0
    assert app._last_reconnect_reason == "connection_closed"


def test_recorder_reconnect_resets_subscription_state_without_clearing_price_cache(tmp_path):
    settings = Settings(
        watch_symbols=("NVDA",),
        benchmark_symbols=("SPY",),
        database_path=tmp_path / "reset-state.db",
        fetch_article_body=False,
    )
    app = IBKRMarketEventRecorder(settings)
    app._contracts["NVDA"] = Contract()
    app._contract_req_to_symbol[1001] = "NVDA"
    app._contract_requests_completed.add(1001)
    app._market_req_to_symbol[2001] = "NVDA"
    app._news_req_to_symbol[3001] = "NVDA"
    app._broadtape_req_to_provider[4001] = "BZ"
    app._article_req_to_news_id[5001] = 1
    app._providers = {"BZ": "Benzinga"}
    app._providers_received = True
    app._subscriptions_started = True
    now = datetime.now(UTC)
    app.prices.put_snapshot(QuoteSnapshot("NVDA", now, bid=99.0, ask=101.0, last=100.0))

    app._reset_subscription_state_for_reconnect()

    assert app._contracts == {}
    assert app._contract_req_to_symbol == {}
    assert app._contract_requests_completed == set()
    assert app._market_req_to_symbol == {}
    assert app._news_req_to_symbol == {}
    assert app._broadtape_req_to_provider == {}
    assert app._article_req_to_news_id == {}
    assert app._providers == {}
    assert app._providers_received is False
    assert app._subscriptions_started is False
    assert app.prices.get("NVDA") is not None
