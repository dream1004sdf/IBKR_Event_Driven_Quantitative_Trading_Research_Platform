from __future__ import annotations

from datetime import datetime, timezone
import time

from recorder.market_data_desk import (
    DEFAULT_MARKET_DATA_INSTRUMENTS,
    MarketDataDeskApp,
    MarketDataState,
    account_option_underlying_symbols,
    contract_candidate_count,
    contract_candidate_label,
    contract_for_instrument,
    format_market_data_report,
    market_data_instruments_with_account_option_underlyings,
    market_data_desk_summary,
    market_data_snapshot_payload,
    refresh_selected_price,
    update_alerts,
)
from recorder.market_data_session_overlay import (
    build_market_data_session_overlay,
    evaluate_us_market_session,
    market_data_session_overlay_markdown,
)


def _instrument(key: str):
    return next(item for item in DEFAULT_MARKET_DATA_INSTRUMENTS if item.key == key)


def test_market_data_desk_contracts_cover_requested_global_watchlist():
    keys = {item.key for item in DEFAULT_MARKET_DATA_INSTRUMENTS}
    assert {
        "DXY",
        "USDKRW",
        "USDJPY",
        "USDCNH",
        "EURUSD",
        "GBPUSD",
        "US03M",
        "US02Y",
        "US10Y",
        "US30Y",
        "VIX",
        "VVIX",
        "KOSPI",
        "KOSPI_FUT",
        "KOSDAQ",
        "NIKKEI",
        "ES",
        "NQ",
        "YM",
        "RTY",
        "GC",
        "SI",
        "WTI",
        "BRENT",
        "BTC",
        "ETH",
    } <= keys

    usdkrw = contract_for_instrument(_instrument("USDKRW"))
    assert usdkrw.secType == "CASH"
    assert usdkrw.symbol == "USD"
    assert usdkrw.currency == "KRW"

    es = contract_for_instrument(_instrument("ES"))
    assert es.secType == "FUT"
    assert es.exchange == "CME"

    dxy = contract_for_instrument(_instrument("DXY"))
    assert dxy.secType == "STK"
    assert dxy.symbol == "UUP"
    assert dxy.primaryExchange == "ARCA"

    us02y = contract_for_instrument(_instrument("US02Y"))
    assert us02y.secType == "STK"
    assert us02y.symbol == "UTWO"
    assert us02y.exchange == "SMART"
    assert us02y.primaryExchange == "ARCA"
    assert contract_candidate_count(_instrument("US02Y")) >= 4

    us02y_shy = contract_for_instrument(_instrument("US02Y"), candidate_index=1)
    assert us02y_shy.secType == "STK"
    assert us02y_shy.symbol == "SHY"
    assert us02y_shy.exchange == "SMART"
    assert us02y_shy.primaryExchange == "NASDAQ"

    us02y_2yy = contract_for_instrument(_instrument("US02Y"), candidate_index=2)
    assert us02y_2yy.secType == "FUT"
    assert us02y_2yy.symbol == "2YY"
    assert us02y_2yy.exchange == "CBOT"

    us02y_zt = contract_for_instrument(_instrument("US02Y"), candidate_index=3)
    assert us02y_zt.secType == "FUT"
    assert us02y_zt.symbol == "ZT"
    assert us02y_zt.exchange == "CBOT"

    kosdaq = contract_for_instrument(_instrument("KOSDAQ"))
    assert kosdaq.secType == "IND"
    assert kosdaq.symbol == "KOSDQ150"

    btc = contract_for_instrument(_instrument("BTC"))
    assert btc.secType == "STK"
    assert btc.symbol == "BITO"
    assert btc.primaryExchange == "ARCA"

    eth = contract_for_instrument(_instrument("ETH"))
    assert eth.secType == "STK"
    assert eth.symbol == "ETHA"
    assert eth.primaryExchange == "NASDAQ"

    eurusd = _instrument("EURUSD")
    assert contract_candidate_count(eurusd) >= 3
    assert contract_candidate_label(eurusd, 1) == "idealpro_local_symbol"
    eurusd_local = contract_for_instrument(eurusd, candidate_index=1)
    assert eurusd_local.localSymbol == "EUR.USD"
    assert eurusd_local.exchange == "IDEALPRO"
    eurusd_future = contract_for_instrument(eurusd, candidate_index=2)
    assert eurusd_future.secType == "FUT"
    assert eurusd_future.symbol == "M6E"
    assert eurusd_future.exchange == "CME"

    gbpusd_future = contract_for_instrument(_instrument("GBPUSD"), candidate_index=2)
    assert gbpusd_future.secType == "FUT"
    assert gbpusd_future.symbol == "M6B"

    usdcnh_future = contract_for_instrument(_instrument("USDCNH"), candidate_index=2)
    assert usdcnh_future.secType == "FUT"
    assert usdcnh_future.symbol == "RMB"


def test_market_data_desk_adds_account_option_underlying_symbols(tmp_path):
    account = tmp_path / "account.json"
    account.write_text(
        """
        {
          "positions": [
            {"contract": {"symbol": "EWY", "sec_type": "OPT", "currency": "USD"}, "position": 1},
            {"contract": {"symbol": "SPY", "sec_type": "STK", "currency": "USD"}, "position": 1},
            {"contract": {"symbol": "EWY", "sec_type": "OPT", "currency": "USD"}, "position": 2}
          ]
        }
        """,
        encoding="utf-8",
    )

    assert account_option_underlying_symbols(account) == ("EWY",)
    instruments = market_data_instruments_with_account_option_underlyings(
        extra_stock_symbols="MSFT,EWY",
        account_snapshot_path=account,
    )
    by_key = {item.key: item for item in instruments}

    assert by_key["EWY"].sec_type == "STK"
    assert by_key["EWY"].exchange == "SMART"
    assert by_key["EWY"].desk_group == "account_option_underlying"
    assert by_key["MSFT"].sec_type == "STK"
    assert list(item.key for item in instruments).count("EWY") == 1


def test_market_data_desk_alerts_gap_move_and_delayed_data():
    state = MarketDataState(instrument=_instrument("VIX"))
    state.last = 22.5
    state.close = 20.0
    state.market_data_type = 3
    state.max_tick_move_pct = 0.75
    state.timestamp = datetime(2026, 6, 28, tzinfo=timezone.utc)
    refresh_selected_price(state)
    update_alerts(state, gap_threshold_pct=1.0, tick_move_threshold_pct=0.5)

    assert state.selected_price == 22.5
    assert "delayed_market_data" in state.alerts
    assert any(item.startswith("gap_up:") for item in state.alerts)
    assert any(item.startswith("sudden_move:") for item in state.alerts)


def test_market_data_desk_does_not_treat_close_to_first_live_tick_as_sudden_move():
    app = MarketDataDeskApp(instruments=(_instrument("KOSPI"),), tick_move_threshold_pct=0.5)
    app.states["KOSPI"].status = "subscribed"
    app._market_req_to_key[9100] = "KOSPI"

    app.tickPrice(9100, 9, 100.0, None)
    app.tickPrice(9100, 4, 105.0, None)

    state = app.states["KOSPI"]
    assert state.close == 100.0
    assert state.last == 105.0
    assert state.max_tick_move_pct == 0.0
    assert any(item.startswith("gap_up:") for item in state.alerts)
    assert not any(item.startswith("sudden_move:") for item in state.alerts)

    app.tickPrice(9100, 4, 106.0, None)

    assert state.max_tick_move_pct > 0.5
    assert any(item.startswith("sudden_move:") for item in state.alerts)


def test_market_data_desk_report_summarizes_delay_alerts_and_errors():
    delayed = MarketDataState(instrument=_instrument("BTC"))
    delayed.last = 65000
    delayed.close = 64000
    delayed.market_data_type = 4
    refresh_selected_price(delayed)
    update_alerts(delayed)

    unresolved = MarketDataState(instrument=_instrument("US02Y"))
    unresolved.status = "contract_unresolved"
    unresolved.error = "No IBKR contract details returned"

    rows = []
    for state in (delayed, unresolved):
        rows.append(
            {
                "key": state.instrument.key,
                "label": state.instrument.label,
                "group": state.instrument.desk_group,
                "status": state.status,
                "price": state.selected_price,
                "price_source": state.selected_source,
                "bid": state.bid,
                "ask": state.ask,
                "last": state.last,
                "close": state.close,
                "volume": state.volume,
                "market_data_type": state.market_data_type,
                "market_data_type_label": "delayed_frozen" if state.market_data_type == 4 else "unknown",
                "delayed": state.market_data_type in {3, 4} or state.delayed,
                "alerts": list(state.alerts),
                "error": state.error,
                "timestamp": "",
            }
        )

    report = format_market_data_report(rows)
    assert "BTC" in report
    assert "delayed_instruments=BTC" in report
    assert "alerted_instruments=BTC" in report
    assert "error_or_unresolved=US02Y" in report
    assert "no account, portfolio, or order action" in report


def test_market_data_desk_temperature_snapshot_scores_data_quality_and_moves():
    rows = [
        {
            "key": "ES",
            "label": "E-mini S&P 500",
            "group": "us_equity_futures",
            "status": "live",
            "price": 6100.0,
            "price_source": "last",
            "bid": None,
            "ask": None,
            "last": 6100.0,
            "close": 6000.0,
            "gap_pct": 1.6667,
            "tick_move_pct": 0.2,
            "volume": None,
            "market_data_type": 1,
            "market_data_type_label": "real_time",
            "delayed": False,
            "alerts": ["gap_up:+1.67%"],
            "error": "",
            "timestamp": "2026-06-30T14:00:00+00:00",
        },
        {
            "key": "VIX",
            "label": "CBOE VIX",
            "group": "volatility",
            "status": "live",
            "price": 22.0,
            "price_source": "last",
            "bid": None,
            "ask": None,
            "last": 22.0,
            "close": 21.0,
            "gap_pct": 4.7619,
            "tick_move_pct": 0.8,
            "volume": None,
            "market_data_type": 3,
            "market_data_type_label": "delayed",
            "delayed": True,
            "alerts": ["delayed_market_data", "sudden_move:0.80%"],
            "error": "",
            "timestamp": "2026-06-30T14:00:00+00:00",
        },
        {
            "key": "US02Y",
            "label": "US 2Y Treasury futures",
            "group": "rates",
            "status": "contract_unresolved",
            "price": None,
            "price_source": "",
            "bid": None,
            "ask": None,
            "last": None,
            "close": None,
            "market_data_type": None,
            "market_data_type_label": "unknown",
            "delayed": False,
            "alerts": [],
            "error": "No IBKR contract details returned",
            "timestamp": "",
        },
    ]

    summary = market_data_desk_summary(rows)
    payload = market_data_snapshot_payload(rows)

    assert summary["status"] == "red"
    assert summary["temperature_label"] == "stress"
    assert summary["delayed_count"] == 1
    assert summary["missing_price_count"] == 1
    assert summary["error_count"] == 1
    assert summary["alert_count"] == 2
    assert summary["group_summaries"]["volatility"]["status"] == "yellow"
    assert summary["group_summaries"]["rates"]["status"] == "red"
    assert payload["market_data_temperature_version"] == "market-data-temperature-v1"
    assert payload["temperature"]["label"] == "stress"
    assert payload["safety"]["read_only_ibkr_market_data"] is True


def test_market_data_session_overlay_keeps_stock_overnight_monitoring_while_deferring_options():
    payload = market_data_snapshot_payload(
        [
            {
                "key": "BTC",
                "label": "Bitcoin proxy / BITO ETF",
                "group": "crypto",
                "status": "live",
                "price": 100.0,
                "price_source": "last",
                "bid": None,
                "ask": None,
                "last": 100.0,
                "close": 95.0,
                "gap_pct": 5.26,
                "tick_move_pct": 0.0,
                "volume": None,
                "market_data_type": 1,
                "market_data_type_label": "real_time",
                "delayed": False,
                "alerts": ["gap_up:+5.26%"],
                "error": "",
                "timestamp": "2026-07-01T02:00:00+00:00",
            },
            {
                "key": "ES",
                "label": "E-mini S&P 500 front month",
                "group": "us_equity_futures",
                "status": "live",
                "price": 6100.0,
                "price_source": "last",
                "bid": None,
                "ask": None,
                "last": 6100.0,
                "close": 6000.0,
                "gap_pct": 1.66,
                "tick_move_pct": 0.0,
                "volume": None,
                "market_data_type": 1,
                "market_data_type_label": "real_time",
                "delayed": False,
                "alerts": ["gap_up:+1.66%"],
                "error": "",
                "timestamp": "2026-07-01T02:00:00+00:00",
            },
            {
                "key": "VIX",
                "label": "CBOE VIX",
                "group": "volatility",
                "status": "live",
                "price": 22.0,
                "price_source": "last",
                "bid": None,
                "ask": None,
                "last": 22.0,
                "close": 21.0,
                "gap_pct": 4.76,
                "tick_move_pct": 0.0,
                "volume": None,
                "market_data_type": 1,
                "market_data_type_label": "real_time",
                "delayed": False,
                "alerts": ["gap_up:+4.76%"],
                "error": "",
                "timestamp": "2026-07-01T02:00:00+00:00",
            },
        ]
    )

    session = evaluate_us_market_session(now="2026-07-01T21:30:00-04:00")
    overlay = build_market_data_session_overlay(payload, now="2026-07-01T21:30:00-04:00")
    markdown = market_data_session_overlay_markdown(overlay)

    assert session["session"] == "overnight"
    assert overlay["risk_interpretation"] == "overnight_monitoring_active"
    assert overlay["stock_cash_overnight_monitoring_enabled"] is True
    assert overlay["option_quotes_regular_session_dependent"] is True
    assert "BTC" in overlay["summary"]["monitorable_alert_instruments"]
    assert "ES" in overlay["summary"]["monitorable_alert_instruments"]
    assert "VIX" in overlay["summary"]["regular_dependent_alert_instruments"]
    assert "Stock/ETF cash instruments" in markdown


def test_market_data_desk_treats_subscribed_no_price_as_warming_up_not_missing():
    rows = [
        {
            "key": "EURUSD",
            "label": "EUR/USD",
            "group": "fx",
            "status": "subscribed",
            "price": None,
            "price_source": "",
            "bid": None,
            "ask": None,
            "last": None,
            "close": None,
            "market_data_type": None,
            "market_data_type_label": "unknown",
            "delayed": False,
            "alerts": [],
            "error": "",
            "timestamp": "",
        }
    ]

    summary = market_data_desk_summary(rows)
    report = format_market_data_report(rows)

    assert summary["missing_price_count"] == 0
    assert summary["warming_up_count"] == 1
    assert summary["warming_up_instruments"] == ["EURUSD"]
    assert summary["status"] == "green"
    assert "market_data_subscriptions_warming_up" in summary["risk_flags"]
    assert "warming_up_instruments=EURUSD" in report


def test_market_data_desk_treats_contract_pending_futures_as_warming_up_not_missing():
    rows = [
        {
            "key": "GC",
            "label": "Gold futures front month",
            "group": "commodities",
            "status": "contract_pending",
            "price": None,
            "price_source": "",
            "bid": None,
            "ask": None,
            "last": None,
            "close": None,
            "market_data_type": None,
            "market_data_type_label": "unknown",
            "delayed": False,
            "alerts": [],
            "error": "",
            "timestamp": "",
        }
    ]

    summary = market_data_desk_summary(rows)

    assert summary["missing_price_count"] == 0
    assert summary["warming_up_count"] == 1
    assert summary["warming_up_instruments"] == ["GC"]
    assert "missing_market_prices" not in summary["risk_flags"]
    assert "market_data_subscriptions_warming_up" in summary["risk_flags"]


def test_market_data_desk_excludes_wide_spread_gap_from_scored_market_move():
    rows = [
        {
            "key": "NIKKEI",
            "label": "Nikkei 225 futures",
            "group": "asia_equity",
            "status": "live",
            "price": 67850.0,
            "price_source": "midpoint",
            "bid": 60650.0,
            "ask": 75050.0,
            "last": None,
            "close": 70685.0,
            "gap_pct": -4.0108,
            "tick_move_pct": 0.0,
            "quote_spread_pct": 21.2233,
            "live_selected_update_count": 1,
            "selected_source_is_close": False,
            "market_data_type": 1,
            "market_data_type_label": "real_time",
            "delayed": False,
            "alerts": ["wide_spread:21.22%", "gap_down:-4.01%"],
            "error": "",
            "timestamp": "2026-06-30T14:00:00+00:00",
        }
    ]

    summary = market_data_desk_summary(rows)

    assert summary["market_move_alert_count"] == 1
    assert summary["scored_market_move_alert_count"] == 0
    assert summary["quality_excluded_market_move_alert_count"] == 1
    assert summary["component_temperatures"]["market_move"]["score"] == 0.0
    assert "quality_excluded_market_move_alerts" in summary["risk_flags"]


def test_market_data_desk_uses_instrument_specific_stale_thresholds_and_importance_weights():
    rows = [
        {
            "key": "US02Y",
            "label": "US 2Y Treasury live proxy",
            "group": "rates",
            "status": "live",
            "price": 47.99,
            "price_source": "last",
            "selected_source_is_close": False,
            "quote_age_seconds": 240.0,
            "delayed": False,
            "alerts": [],
            "error": "",
        },
        {
            "key": "SPY",
            "label": "SPDR S&P 500 ETF Trust",
            "group": "us_equity_index_etf",
            "status": "live",
            "price": 750.0,
            "price_source": "last",
            "selected_source_is_close": False,
            "quote_age_seconds": 120.0,
            "delayed": False,
            "alerts": [],
            "error": "",
        },
        {
            "key": "KOSPI",
            "label": "KOSPI 200 Index",
            "group": "asia_equity",
            "status": "live",
            "price": 1219.62,
            "close": 1335.34,
            "gap_pct": -8.67,
            "price_source": "last",
            "selected_source_is_close": False,
            "quote_age_seconds": 600.0,
            "delayed": False,
            "alerts": ["gap_down:-8.67%"],
            "error": "",
        },
        {
            "key": "KOSPI_FUT",
            "label": "KOSPI 200 Futures",
            "group": "asia_equity",
            "status": "live",
            "price": 1287.0,
            "close": 1240.0,
            "gap_pct": 3.79,
            "price_source": "last",
            "selected_source_is_close": False,
            "quote_age_seconds": 60.0,
            "delayed": False,
            "alerts": ["gap_up:+3.79%"],
            "error": "",
        },
        {
            "key": "KOSDAQ",
            "label": "KOSDAQ 150 Index",
            "group": "asia_equity",
            "status": "live",
            "price": 1674.35,
            "close": 1600.0,
            "gap_pct": 4.65,
            "price_source": "close",
            "selected_source_is_close": True,
            "quote_age_seconds": 600.0,
            "delayed": False,
            "alerts": ["gap_up:+4.65%"],
            "error": "",
        },
        {
            "key": "BRENT",
            "label": "Brent futures",
            "group": "commodities",
            "status": "live",
            "price": 72.95,
            "price_source": "close",
            "selected_source_is_close": True,
            "quote_age_seconds": 600.0,
            "delayed": False,
            "alerts": [],
            "error": "",
        },
    ]

    summary = market_data_desk_summary(rows)

    assert "US02Y" not in summary["stale_instruments"]
    assert "SPY" in summary["stale_instruments"]
    assert "KOSPI" not in summary["stale_instruments"]
    assert "BRENT" not in summary["stale_instruments"]
    assert summary["stale_threshold_overrides"]["US02Y"] == 300.0
    assert summary["stale_threshold_overrides"]["KOSPI"] == 1800.0
    assert "BRENT" in summary["reference_close_only_instruments"]
    assert "KOSPI" in summary["monitoring_only_instruments"]
    assert [item["key"] for item in summary["verified_reference_moves"]] == ["KOSPI", "KOSPI_FUT", "KOSDAQ"]
    assert all(item["trade_blocking"] is False for item in summary["verified_reference_moves"])
    assert all(item["factual_asia_reference_key"] is True for item in summary["verified_reference_moves"])
    kospi_move = next(item for item in summary["top_moves"] if item["key"] == "KOSPI")
    assert kospi_move["market_data_importance"] == "us_trading_reference"
    assert kospi_move["market_data_impact_weight"] == 0.3
    assert rows[0]["stale_threshold_seconds"] == 300.0


def test_market_data_desk_retries_next_contract_candidate_when_no_price():
    app = MarketDataDeskApp(instruments=(_instrument("EURUSD"),), fallback_after_seconds=1.0)
    contract_detail_requests = []
    cancelled_market_data = []
    app.reqContractDetails = lambda req_id, contract: contract_detail_requests.append((req_id, contract))  # type: ignore[method-assign]
    app.cancelMktData = lambda req_id: cancelled_market_data.append(req_id)  # type: ignore[method-assign]

    state = app.states["EURUSD"]
    state.status = "subscribed"
    state.active_candidate_index = 0
    state.active_candidate_label = "base"
    state.attempted_candidate_labels = ["base"]
    state.subscription_started_monotonic = time.monotonic() - 5.0
    app._active_market_req_by_key["EURUSD"] = 9001
    app._market_req_to_key[9001] = "EURUSD"

    app._retry_no_price_subscriptions(now=time.monotonic())

    assert cancelled_market_data == [9001]
    assert contract_detail_requests
    _, retry_contract = contract_detail_requests[-1]
    assert retry_contract.localSymbol == "EUR.USD"
    assert state.active_candidate_index == 1
    assert state.active_candidate_label == "idealpro_local_symbol"
    assert "idealpro_local_symbol" in state.attempted_candidate_labels
    assert any("no_price_after_1" in item for item in state.candidate_retry_reasons)


def test_market_data_desk_retries_next_contract_candidate_when_close_only():
    app = MarketDataDeskApp(
        instruments=(_instrument("US02Y"),),
        fallback_after_seconds=1.0,
        fallback_on_close_only=True,
    )
    contract_detail_requests = []
    cancelled_market_data = []
    app.reqContractDetails = lambda req_id, contract: contract_detail_requests.append((req_id, contract))  # type: ignore[method-assign]
    app.cancelMktData = lambda req_id: cancelled_market_data.append(req_id)  # type: ignore[method-assign]

    state = app.states["US02Y"]
    state.status = "live"
    state.active_candidate_index = 0
    state.active_candidate_label = "base"
    state.attempted_candidate_labels = ["base"]
    state.subscription_started_monotonic = time.monotonic() - 5.0
    state.close = 48.0
    state.selected_price = 48.0
    state.selected_source = "close"
    state.close_tick_count = 1
    state.non_close_price_tick_count = 0
    app._active_market_req_by_key["US02Y"] = 9001
    app._market_req_to_key[9001] = "US02Y"

    app._retry_no_price_subscriptions(now=time.monotonic())

    assert cancelled_market_data == [9001]
    assert contract_detail_requests
    _, retry_contract = contract_detail_requests[-1]
    assert retry_contract.symbol == "SHY"
    assert retry_contract.secType == "STK"
    assert retry_contract.primaryExchange == "NASDAQ"
    assert state.active_candidate_index == 1
    assert state.active_candidate_label == "shy_1_3y_treasury_etf_proxy"
    assert any("close_only_after_1" in item for item in state.candidate_retry_reasons)


def test_market_data_desk_keeps_close_only_price_by_default():
    app = MarketDataDeskApp(instruments=(_instrument("US02Y"),), fallback_after_seconds=1.0)
    contract_detail_requests = []
    cancelled_market_data = []
    app.reqContractDetails = lambda req_id, contract: contract_detail_requests.append((req_id, contract))  # type: ignore[method-assign]
    app.cancelMktData = lambda req_id: cancelled_market_data.append(req_id)  # type: ignore[method-assign]

    state = app.states["US02Y"]
    state.status = "live"
    state.active_candidate_index = 0
    state.active_candidate_label = "base"
    state.attempted_candidate_labels = ["base"]
    state.subscription_started_monotonic = time.monotonic() - 5.0
    state.close = 48.0
    state.selected_price = 48.0
    state.selected_source = "close"
    state.close_tick_count = 1
    state.non_close_price_tick_count = 0
    app._active_market_req_by_key["US02Y"] = 9001
    app._market_req_to_key[9001] = "US02Y"

    app._retry_no_price_subscriptions(now=time.monotonic())

    assert cancelled_market_data == []
    assert contract_detail_requests == []
    assert state.active_candidate_index == 0
    assert state.active_candidate_label == "base"
    assert state.selected_price == 48.0
    assert not any("close_only_after_1" in item for item in state.candidate_retry_reasons)


def test_market_data_desk_can_retry_to_fx_future_proxy_and_invert_cnh_price():
    app = MarketDataDeskApp(instruments=(_instrument("USDCNH"),), fallback_after_seconds=1.0)
    contract_detail_requests = []
    cancelled_market_data = []
    app.reqContractDetails = lambda req_id, contract: contract_detail_requests.append((req_id, contract))  # type: ignore[method-assign]
    app.cancelMktData = lambda req_id: cancelled_market_data.append(req_id)  # type: ignore[method-assign]

    state = app.states["USDCNH"]
    state.status = "subscribed"
    state.active_candidate_index = 1
    state.active_candidate_label = "idealpro_local_symbol"
    state.attempted_candidate_labels = ["base", "idealpro_local_symbol"]
    state.subscription_started_monotonic = time.monotonic() - 5.0
    app._active_market_req_by_key["USDCNH"] = 9001
    app._market_req_to_key[9001] = "USDCNH"

    app._retry_no_price_subscriptions(now=time.monotonic())

    assert cancelled_market_data == [9001]
    _, retry_contract = contract_detail_requests[-1]
    assert retry_contract.symbol == "RMB"
    assert retry_contract.secType == "FUT"
    assert state.active_candidate_index == 2
    assert state.active_candidate_label == "rmb_future_proxy_inverted"
    assert state.active_candidate_invert_price is True

    app._market_req_to_key[9100] = "USDCNH"
    app.tickPrice(9100, 4, 0.14676, None)

    assert state.last == 1.0 / 0.14676
    assert state.selected_price == state.last


def test_market_data_desk_ignores_ibkr_fractional_size_warning():
    app = MarketDataDeskApp(instruments=(_instrument("EURUSD"),))
    app.states["EURUSD"].status = "subscribed"
    app._market_req_to_key[9001] = "EURUSD"

    app.error(9001, 10285, "Your API version does not support fractional size rules")

    assert app.states["EURUSD"].error == ""
    assert app.states["EURUSD"].status == "subscribed"
