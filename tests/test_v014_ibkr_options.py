from __future__ import annotations

from datetime import date

from recorder.ibkr_options import (
    OptionChainScanConfig,
    OptionQuoteSnapshotConfig,
    _quote_quality,
    option_chain_scan_markdown,
    option_chain_scan_summary,
    option_quote_snapshot_markdown,
    option_quote_snapshot_summary,
    limit_option_candidates_by_symbol,
    parse_reference_prices,
    select_option_chain_candidates,
)
from recorder.ibkr_account import (
    AccountSnapshotConfig,
    account_snapshot_markdown,
    account_snapshot_summary,
)
from recorder.ibkr_stock_marks import (
    StockMarkSnapshotConfig,
    stock_mark_snapshot_markdown,
    stock_mark_snapshot_summary,
    stock_mark_symbols_from_router_payload,
)


def test_parse_reference_prices_requires_symbol_price_pairs() -> None:
    values = parse_reference_prices("NVDA=125.5, tsla=250")

    assert values == {"NVDA": 125.5, "TSLA": 250.0}


def test_select_option_chain_candidates_around_reference_price() -> None:
    chains = [
        {
            "symbol": "NVDA",
            "exchange": "SMART",
            "underlying_con_id": 123,
            "trading_class": "NVDA",
            "multiplier": "100",
            "expirations": ["20260717", "20260821", "20270115"],
            "strikes": [90, 95, 100, 105, 110, 115],
        }
    ]

    candidates = select_option_chain_candidates(
        chains,
        reference_prices={"NVDA": 103.0},
        min_dte=7,
        max_dte=60,
        strikes_around=1,
        max_expirations=2,
        as_of=date(2026, 6, 29),
    )

    assert len(candidates) == 12
    assert {row["expiration"] for row in candidates} == {"20260717", "20260821"}
    assert {row["strike"] for row in candidates} == {100.0, 105.0, 110.0}
    assert {row["right"] for row in candidates} == {"C", "P"}
    assert all(row["safety"]["ibkr_order_submitted"] is False for row in candidates)


def test_option_chain_scan_summary_and_markdown() -> None:
    config = OptionChainScanConfig(
        symbols=("NVDA",),
        reference_prices={"NVDA": 103.0},
        min_dte=7,
        max_dte=60,
        strikes_around=1,
        max_expirations=2,
    )
    chains = [
        {
            "symbol": "NVDA",
            "exchange": "SMART",
            "underlying_con_id": 123,
            "trading_class": "NVDA",
            "multiplier": "100",
            "expirations": ["20260717", "20260821"],
            "strikes": [100, 105, 110],
        }
    ]
    candidates = select_option_chain_candidates(
        chains,
        reference_prices={"NVDA": 103.0},
        min_dte=7,
        max_dte=60,
        strikes_around=1,
        max_expirations=2,
        as_of=date(2026, 6, 29),
    )

    summary = option_chain_scan_summary(
        symbols=("NVDA",),
        underlying_contracts=[{"symbol": "NVDA", "con_id": 123}],
        chains=chains,
        candidates=candidates,
        errors=[],
        config=config,
        completed=True,
    )
    report = option_chain_scan_markdown(summary)

    assert summary["completed"] is True
    assert summary["by_symbol"][0]["candidate_contracts"] == 12
    assert "IBKR Option Chain Read-Only Scan Report" in report


def test_option_quote_snapshot_summary_and_markdown() -> None:
    config = OptionQuoteSnapshotConfig(
        symbols=("NVDA",),
        reference_prices={"NVDA": 120.0},
        max_quote_contracts=2,
        max_spread_pct=0.25,
    )
    quote = {
        "symbol": "NVDA",
        "expiration": "20260717",
        "dte": 18,
        "right": "C",
        "strike": 120.0,
        "moneyness": 0.0,
        "request_id": 800001,
        "market_data_type": 3,
        "delayed": True,
        "bid": 1.0,
        "ask": 1.2,
        "last": 1.1,
        "close": 1.0,
    }
    quote.update(_quote_quality(quote, max_spread_pct=config.max_spread_pct))

    summary = option_quote_snapshot_summary(
        chain_summary={"candidate_contracts": [quote], "errors": []},
        quotes=[quote],
        errors=[],
        config=config,
    )
    report = option_quote_snapshot_markdown(summary)

    assert summary["requested_quotes"] == 1
    assert summary["quoted_contracts"] == 1
    assert summary["quoted_rate"] == 1.0
    assert summary["status_counts"]["quoted"] == 1
    assert summary["safety"]["ibkr_order_submitted"] is False
    assert "IBKR Option Quote Read-Only Snapshot Report" in report
    assert "Close" in report


def test_limit_option_candidates_by_symbol_balances_symbols() -> None:
    candidates = [
        {"symbol": "NVDA", "id": 1},
        {"symbol": "NVDA", "id": 2},
        {"symbol": "NVDA", "id": 3},
        {"symbol": "TSLA", "id": 4},
        {"symbol": "TSLA", "id": 5},
    ]

    selected = limit_option_candidates_by_symbol(candidates, 4)

    assert [row["id"] for row in selected] == [1, 4, 2, 5]


def test_account_snapshot_summary_is_read_only_and_extracts_budget_values() -> None:
    config = AccountSnapshotConfig(
        account="DU123456",
        include_positions=False,
        include_open_orders=False,
    )
    summary = account_snapshot_summary(
        account_summary=[
            {"account": "DU123456", "tag": "NetLiquidation", "value": "100000.25", "currency": "USD"},
            {"account": "DU123456", "tag": "TotalCashValue", "value": "75000.50", "currency": "USD"},
            {"account": "DU123456", "tag": "BuyingPower", "value": "200000.00", "currency": "USD"},
        ],
        positions=[],
        open_orders=[],
        errors=[],
        config=config,
        next_order_id_seen=1234,
    )
    report = account_snapshot_markdown(summary)

    assert summary["equity"] == 100000.25
    assert summary["cash"] == 75000.50
    assert summary["buying_power"] == 200000.00
    assert summary["safety"]["ibkr_order_submitted"] is False
    assert summary["safety"]["next_order_id_seen_but_unused"] == 1234
    assert "IBKR Account Read-Only Snapshot Report" in report


def test_account_snapshot_summary_selects_largest_equity_when_account_not_specified() -> None:
    summary = account_snapshot_summary(
        account_summary=[
            {"account": "DU_SMALL", "tag": "NetLiquidation", "value": "1000", "currency": "USD"},
            {"account": "DU_LARGE", "tag": "NetLiquidation", "value": "5000", "currency": "USD"},
            {"account": "DU_LARGE", "tag": "TotalCashValue", "value": "4000", "currency": "USD"},
        ],
        positions=[],
        open_orders=[],
        errors=[],
        config=AccountSnapshotConfig(account=None),
        next_order_id_seen=None,
    )

    assert summary["selected_account"] == "DU_LARGE"
    assert summary["equity"] == 5000.0
    assert summary["cash"] == 4000.0


def test_stock_mark_snapshot_summary_prefers_last_then_midpoint_and_is_read_only() -> None:
    config = StockMarkSnapshotConfig(symbols=("NFLX", "TSLA"), market_data_type=3)
    summary = stock_mark_snapshot_summary(
        rows=[
            {
                "request_id": 932100,
                "symbol": "NFLX",
                "bid": 104.0,
                "ask": 106.0,
                "last": 105.5,
                "close": 100.0,
                "market_data_type": 3,
                "market_data_type_label": "delayed",
                "delayed": True,
            },
            {
                "request_id": 932101,
                "symbol": "TSLA",
                "bid": 94.0,
                "ask": 96.0,
                "last": None,
                "close": 100.0,
                "market_data_type": 1,
                "market_data_type_label": "live",
                "delayed": False,
            },
        ],
        errors=[],
        config=config,
    )
    report = stock_mark_snapshot_markdown(summary)

    assert summary["marked_count"] == 2
    assert summary["marks"][0]["symbol"] == "NFLX"
    assert summary["marks"][0]["mark_price"] == 105.5
    assert summary["marks"][0]["mark_source"] == "ibkr_last_delayed"
    assert summary["marks"][1]["symbol"] == "TSLA"
    assert summary["marks"][1]["mark_price"] == 95.0
    assert summary["marks"][1]["mark_source"] == "ibkr_midpoint"
    assert summary["safety"]["ibkr_order_submitted"] is False
    assert "IBKR Stock Mark Read-Only Snapshot" in report


def test_stock_mark_symbols_from_router_payload_deduplicates_positions() -> None:
    payload = {
        "result": {
            "paper_positions": [
                {"symbol": "nflx"},
                {"symbol": "TSLA"},
                {"symbol": "NFLX"},
                {"symbol": ""},
            ]
        }
    }

    assert stock_mark_symbols_from_router_payload(payload) == ("NFLX", "TSLA")
