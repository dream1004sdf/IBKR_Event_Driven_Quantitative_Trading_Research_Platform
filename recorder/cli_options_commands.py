from __future__ import annotations

from pathlib import Path
from typing import Any


def add_ibkr_option_snapshot_commands(sub: Any) -> None:
    """Register read-only IBKR option chain/quote snapshot commands."""

    ibkr_options_chain_scan = sub.add_parser(
        "ibkr-options-chain-scan",
        help="Read-only IBKR option-chain definition scan using reqSecDefOptParams",
    )
    ibkr_options_chain_scan.add_argument("--symbols", required=True, help="Comma-separated ticker symbols, e.g. NVDA,TSLA")
    ibkr_options_chain_scan.add_argument("--reference-prices", help="Comma-separated SYMBOL=PRICE pairs, e.g. NVDA=125,TSLA=250")
    ibkr_options_chain_scan.add_argument("--underlying-sec-types", help="Optional SYMBOL=SECTYPE map for index options, e.g. SPX=IND,NDX=IND")
    ibkr_options_chain_scan.add_argument("--underlying-exchanges", help="Optional SYMBOL=EXCHANGE map for underlying resolution, e.g. SPX=CBOE,NDX=NASDAQ")
    ibkr_options_chain_scan.add_argument("--min-dte", type=int, default=7)
    ibkr_options_chain_scan.add_argument("--max-dte", type=int, default=60)
    ibkr_options_chain_scan.add_argument("--strikes-around", type=int, default=2)
    ibkr_options_chain_scan.add_argument("--max-expirations", type=int, default=4)
    ibkr_options_chain_scan.add_argument("--timeout", type=float, default=20.0)
    ibkr_options_chain_scan.add_argument("--client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 6000")
    ibkr_options_chain_scan.add_argument("--output", type=Path, help="Optional Markdown option-chain scan report path")
    ibkr_options_chain_scan.add_argument("--json-output-path", type=Path, help="Optional JSON option-chain scan report path")
    ibkr_options_chain_scan.add_argument("--json", action="store_true", help="Print machine-readable option-chain scan JSON")

    ibkr_options_quote_snapshot = sub.add_parser(
        "ibkr-options-quote-snapshot",
        help="Read-only IBKR option quote snapshot for selected chain candidates",
    )
    ibkr_options_quote_snapshot.add_argument("--symbols", required=True, help="Comma-separated ticker symbols, e.g. NVDA,TSLA")
    ibkr_options_quote_snapshot.add_argument("--reference-prices", help="Comma-separated SYMBOL=PRICE pairs, e.g. NVDA=125,TSLA=250")
    ibkr_options_quote_snapshot.add_argument("--underlying-sec-types", help="Optional SYMBOL=SECTYPE map for index options, e.g. SPX=IND,NDX=IND")
    ibkr_options_quote_snapshot.add_argument("--underlying-exchanges", help="Optional SYMBOL=EXCHANGE map for underlying resolution, e.g. SPX=CBOE,NDX=NASDAQ")
    ibkr_options_quote_snapshot.add_argument("--min-dte", type=int, default=7)
    ibkr_options_quote_snapshot.add_argument("--max-dte", type=int, default=60)
    ibkr_options_quote_snapshot.add_argument("--strikes-around", type=int, default=1)
    ibkr_options_quote_snapshot.add_argument("--max-expirations", type=int, default=2)
    ibkr_options_quote_snapshot.add_argument("--max-quote-contracts", type=int, default=20)
    ibkr_options_quote_snapshot.add_argument("--market-data-type", type=int, default=3, choices=(1, 2, 3, 4))
    ibkr_options_quote_snapshot.add_argument("--generic-ticks", default="100,101,104,106")
    ibkr_options_quote_snapshot.add_argument("--chain-timeout", type=float, default=15.0)
    ibkr_options_quote_snapshot.add_argument("--quote-seconds", type=float, default=8.0)
    ibkr_options_quote_snapshot.add_argument("--max-spread-pct", type=float, default=0.25)
    ibkr_options_quote_snapshot.add_argument("--client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 6100")
    ibkr_options_quote_snapshot.add_argument("--output", type=Path, help="Optional Markdown option quote snapshot report path")
    ibkr_options_quote_snapshot.add_argument("--json-output-path", type=Path, help="Optional JSON option quote snapshot report path")
    ibkr_options_quote_snapshot.add_argument("--json", action="store_true", help="Print machine-readable option quote snapshot JSON")


def add_options_market_structure_commands(sub: Any) -> None:
    """Register option market-structure, M222 outcome, and stock-option watchlist commands."""

    options_market_structure = sub.add_parser(
        "options-market-structure-desk",
        help="Build read-only option market-structure overlay from stored option quote snapshots",
    )
    options_market_structure.add_argument("--quote-json", type=Path, required=True, help="Stored IBKR option quote snapshot JSON")
    options_market_structure.add_argument("--market-data-json", type=Path, help="Optional Market Data Desk JSON for fresher underlying prices")
    options_market_structure.add_argument("--pin-near-pct", type=float, default=0.5, help="Distance to max-pain/pin level treated as pin risk")
    options_market_structure.add_argument("--proximity-near-pct", type=float, default=1.0, help="Distance to call/put wall, gamma flip, or max pain treated as a proximity alert")
    options_market_structure.add_argument("--max-snapshot-age-minutes", type=float, default=30.0, help="Option quote snapshot age above this threshold is marked stale/downweighted")
    options_market_structure.add_argument("--no-volume-fallback", action="store_true", help="Do not use option volume as a lower-confidence fallback when open interest is missing")
    options_market_structure.add_argument("--output", type=Path, help="Optional Markdown option market-structure report path")
    options_market_structure.add_argument("--json-output-path", type=Path, help="Optional JSON option market-structure report path")
    options_market_structure.add_argument("--json", action="store_true", help="Print machine-readable option market-structure JSON")

    options_market_structure_supervisor = sub.add_parser(
        "options-market-structure-supervisor",
        help="Run the read-only option quote refresh and market-structure rebuild supervisor",
    )
    options_market_structure_supervisor.add_argument("--profile-json", type=Path, help="Optional M218 options market-structure supervisor profile")
    options_market_structure_supervisor.add_argument("--symbols", help="Comma-separated symbols overriding the profile watchlist, e.g. SPY,QQQ,IWM,DIA")
    options_market_structure_supervisor.add_argument("--max-ticks", type=int)
    options_market_structure_supervisor.add_argument("--interval-seconds", type=float)
    options_market_structure_supervisor.add_argument("--sleep-between-ticks", action="store_true", default=None)
    options_market_structure_supervisor.add_argument("--execute-actions", action="store_true", help="Execute read-only quote refresh/rebuild actions instead of dry-run planning")
    options_market_structure_supervisor.add_argument("--max-snapshot-age-minutes", type=float)
    options_market_structure_supervisor.add_argument("--run-id")
    options_market_structure_supervisor.add_argument("--output", type=Path, help="Optional Markdown supervisor report path")
    options_market_structure_supervisor.add_argument("--json-output-path", type=Path, help="Optional JSON supervisor report path")
    options_market_structure_supervisor.add_argument("--json", action="store_true", help="Print machine-readable supervisor JSON")

    option_structure_outcome_build = sub.add_parser(
        "option-structure-outcome-build",
        help="M222: log option-structure snapshot features against later 5/15/30 minute and EOD market outcomes",
    )
    option_structure_outcome_build.add_argument("--options-market-structure-json", type=Path, required=True)
    option_structure_outcome_build.add_argument("--horizons", default="5M,15M,30M,EOD")
    option_structure_outcome_build.add_argument("--symbols", help="Optional comma-separated symbol filter, e.g. SPY,QQQ,IWM,DIA")
    option_structure_outcome_build.add_argument("--flat-threshold", type=float, default=0.001)
    option_structure_outcome_build.add_argument("--price-source", choices=("ibkr", "none"), default="ibkr")
    option_structure_outcome_build.add_argument("--ibkr-client-id", type=int, help="Override IBKR historical-data clientId")
    option_structure_outcome_build.add_argument("--ibkr-timeout", type=float, default=20.0)
    option_structure_outcome_build.add_argument("--ibkr-what-to-show", default="TRADES")
    option_structure_outcome_build.add_argument("--ibkr-bar-size", default="1 min")
    option_structure_outcome_build.add_argument("--ibkr-all-sessions", action="store_true")
    option_structure_outcome_build.add_argument("--output", type=Path, help="Optional Markdown M222 report path")
    option_structure_outcome_build.add_argument("--json-output-path", type=Path, help="Optional JSON M222 report path")
    option_structure_outcome_build.add_argument("--json", action="store_true", help="Print machine-readable M222 outcome report")

    option_structure_outcome_report = sub.add_parser(
        "option-structure-outcome-report",
        help="Show stored M222 option-structure outcome logs",
    )
    option_structure_outcome_report.add_argument("--limit", type=int, default=50)
    option_structure_outcome_report.add_argument("--symbol")
    option_structure_outcome_report.add_argument("--price-source")
    option_structure_outcome_report.add_argument("--json", action="store_true")

    single_stock_option_watchlist = sub.add_parser(
        "single-stock-option-watchlist",
        help="Build event-triggered single-stock option watchlist and optionally run read-only quote/structure/outcome learning actions",
    )
    single_stock_option_watchlist.add_argument("--profile-json", type=Path, help="Optional single-stock option watchlist profile")
    single_stock_option_watchlist.add_argument("--database-path", type=Path, help="Override market event database path")
    single_stock_option_watchlist.add_argument("--execute-actions", action="store_true", help="Run read-only quote, structure, and outcome commands")
    single_stock_option_watchlist.add_argument("--output", type=Path, help="Optional Markdown report path")
    single_stock_option_watchlist.add_argument("--json-output-path", type=Path, help="Optional JSON report path")
    single_stock_option_watchlist.add_argument("--json", action="store_true", help="Print machine-readable report")

    single_stock_option_watchlist_loop = sub.add_parser(
        "single-stock-option-watchlist-loop",
        help="Continuously run event-triggered single-stock option watchlist learning with read-only actions",
    )
    single_stock_option_watchlist_loop.add_argument("--profile-json", type=Path, help="Optional single-stock option watchlist profile")
    single_stock_option_watchlist_loop.add_argument("--database-path", type=Path, help="Override market event database path")
    single_stock_option_watchlist_loop.add_argument("--execute-actions", action="store_true", help="Run read-only quote, structure, and outcome commands")
    single_stock_option_watchlist_loop.add_argument("--output", type=Path, help="Optional Markdown report path")
    single_stock_option_watchlist_loop.add_argument("--json-output-path", type=Path, help="Optional JSON report path")
    single_stock_option_watchlist_loop.add_argument("--interval-seconds", type=float, default=180.0)
    single_stock_option_watchlist_loop.add_argument("--max-ticks", type=int, default=0, help="0 means run until stopped")
