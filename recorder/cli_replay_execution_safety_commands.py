from __future__ import annotations

from pathlib import Path

from .cli_broker_safety_commands import add_operating_defensive_safety_commands

def add_replay_execution_safety_commands(sub):
    replay_execution_readiness = sub.add_parser(
        "replay-score-execution-readiness",
        help="Build paper pre-execution readiness and ticket drafts from stored stock/option allocation JSON",
    )
    replay_execution_readiness.add_argument("--allocation-json", type=Path, required=True, help="Stock/option allocation or walk-forward JSON path")
    replay_execution_readiness.add_argument("--max-option-max-loss", type=float, default=500.0)
    replay_execution_readiness.add_argument("--max-option-net-debit", type=float, default=500.0)
    replay_execution_readiness.add_argument("--min-option-reward-to-risk", type=float, default=0.20)
    replay_execution_readiness.add_argument("--option-slippage-buffer-pct", type=float, default=0.05)
    replay_execution_readiness.add_argument("--min-option-dte", type=int, default=7)
    replay_execution_readiness.add_argument("--max-stock-notional", type=float, default=5000.0)
    replay_execution_readiness.add_argument("--max-stock-stop-loss", type=float, default=250.0)
    replay_execution_readiness.add_argument("--min-group-rows", type=int, default=1)
    replay_execution_readiness.add_argument("--sample-limit", type=int, default=20)
    replay_execution_readiness.add_argument("--output", type=Path, help="Optional Markdown execution readiness report path")
    replay_execution_readiness.add_argument("--json-output-path", type=Path, help="Optional JSON execution readiness report path")
    replay_execution_readiness.add_argument("--json", action="store_true", help="Print machine-readable execution readiness JSON")
    replay_mock_portfolio_readiness = sub.add_parser(
        "replay-score-mock-portfolio-readiness",
        help="Apply mock account and portfolio budgets to paper execution-readiness ticket drafts",
    )
    replay_mock_portfolio_readiness.add_argument("--readiness-json", type=Path, required=True, help="M108 execution readiness JSON path")
    replay_mock_portfolio_readiness.add_argument("--mock-account-equity", type=float, default=100000.0)
    replay_mock_portfolio_readiness.add_argument("--mock-cash", type=float, default=100000.0)
    replay_mock_portfolio_readiness.add_argument("--max-single-trade-risk-pct", type=float, default=0.005)
    replay_mock_portfolio_readiness.add_argument("--max-total-open-risk-pct", type=float, default=0.05)
    replay_mock_portfolio_readiness.add_argument("--max-gross-notional-pct", type=float, default=1.0)
    replay_mock_portfolio_readiness.add_argument("--max-option-risk-pct", type=float, default=0.02)
    replay_mock_portfolio_readiness.add_argument("--max-short-notional-pct", type=float, default=0.20)
    replay_mock_portfolio_readiness.add_argument("--max-symbol-risk-pct", type=float, default=0.01)
    replay_mock_portfolio_readiness.add_argument("--max-approved-tickets", type=int, default=25)
    replay_mock_portfolio_readiness.add_argument("--sample-limit", type=int, default=20)
    replay_mock_portfolio_readiness.add_argument("--output", type=Path, help="Optional Markdown mock portfolio readiness report path")
    replay_mock_portfolio_readiness.add_argument("--json-output-path", type=Path, help="Optional JSON mock portfolio readiness report path")
    replay_mock_portfolio_readiness.add_argument("--json", action="store_true", help="Print machine-readable mock portfolio readiness JSON")
    replay_account_backed_portfolio_readiness = sub.add_parser(
        "replay-score-account-backed-portfolio-readiness",
        help="Apply read-only IBKR account snapshot budgets to paper execution-readiness ticket drafts",
    )
    replay_account_backed_portfolio_readiness.add_argument("--readiness-json", type=Path, required=True, help="M108 execution readiness JSON path")
    replay_account_backed_portfolio_readiness.add_argument("--account-snapshot-json", type=Path, required=True, help="Read-only IBKR account snapshot JSON path")
    replay_account_backed_portfolio_readiness.add_argument("--equity-haircut-pct", type=float, default=0.0)
    replay_account_backed_portfolio_readiness.add_argument("--cash-haircut-pct", type=float, default=0.0)
    replay_account_backed_portfolio_readiness.add_argument("--max-single-trade-risk-pct", type=float, default=0.005)
    replay_account_backed_portfolio_readiness.add_argument("--max-total-open-risk-pct", type=float, default=0.05)
    replay_account_backed_portfolio_readiness.add_argument("--max-gross-notional-pct", type=float, default=1.0)
    replay_account_backed_portfolio_readiness.add_argument("--max-option-risk-pct", type=float, default=0.02)
    replay_account_backed_portfolio_readiness.add_argument("--max-short-notional-pct", type=float, default=0.20)
    replay_account_backed_portfolio_readiness.add_argument("--max-symbol-risk-pct", type=float, default=0.01)
    replay_account_backed_portfolio_readiness.add_argument("--max-approved-tickets", type=int, default=25)
    replay_account_backed_portfolio_readiness.add_argument("--sample-limit", type=int, default=20)
    replay_account_backed_portfolio_readiness.add_argument("--output", type=Path, help="Optional Markdown account-backed portfolio readiness report path")
    replay_account_backed_portfolio_readiness.add_argument("--json-output-path", type=Path, help="Optional JSON account-backed portfolio readiness report path")
    replay_account_backed_portfolio_readiness.add_argument("--json", action="store_true", help="Print machine-readable account-backed portfolio readiness JSON")
    replay_account_scaled_sizing = sub.add_parser(
        "replay-score-account-scaled-position-sizing",
        help="Resize paper execution-readiness tickets to read-only IBKR account budgets, then re-run portfolio readiness",
    )
    replay_account_scaled_sizing.add_argument("--readiness-json", type=Path, required=True, help="M108 execution readiness JSON path")
    replay_account_scaled_sizing.add_argument("--account-snapshot-json", type=Path, required=True, help="Read-only IBKR account snapshot JSON path")
    replay_account_scaled_sizing.add_argument("--equity-haircut-pct", type=float, default=0.0)
    replay_account_scaled_sizing.add_argument("--cash-haircut-pct", type=float, default=0.0)
    replay_account_scaled_sizing.add_argument("--max-single-trade-risk-pct", type=float, default=0.005)
    replay_account_scaled_sizing.add_argument("--max-total-open-risk-pct", type=float, default=0.05)
    replay_account_scaled_sizing.add_argument("--max-gross-notional-pct", type=float, default=1.0)
    replay_account_scaled_sizing.add_argument("--max-option-risk-pct", type=float, default=0.02)
    replay_account_scaled_sizing.add_argument("--max-short-notional-pct", type=float, default=0.20)
    replay_account_scaled_sizing.add_argument("--max-symbol-risk-pct", type=float, default=0.01)
    replay_account_scaled_sizing.add_argument("--max-stock-notional-pct", type=float, default=0.02)
    replay_account_scaled_sizing.add_argument("--max-cash-per-trade-pct", type=float, default=0.02)
    replay_account_scaled_sizing.add_argument("--sizing-safety-buffer-pct", type=float, default=0.05)
    replay_account_scaled_sizing.add_argument("--no-fractional-shares", action="store_true", help="Disable fractional-share sizing")
    replay_account_scaled_sizing.add_argument("--min-stock-quantity", type=float, default=0.01)
    replay_account_scaled_sizing.add_argument("--stock-quantity-increment", type=float, default=0.01)
    replay_account_scaled_sizing.add_argument("--max-approved-tickets", type=int, default=25)
    replay_account_scaled_sizing.add_argument("--sample-limit", type=int, default=20)
    replay_account_scaled_sizing.add_argument("--output", type=Path, help="Optional Markdown account-scaled sizing report path")
    replay_account_scaled_sizing.add_argument("--json-output-path", type=Path, help="Optional JSON account-scaled sizing report path")
    replay_account_scaled_sizing.add_argument("--json", action="store_true", help="Print machine-readable account-scaled sizing JSON")
    replay_order_preview = sub.add_parser(
        "replay-score-order-preview",
        help="Build paper order-preview and local margin what-if queue from account-scaled position sizing output",
    )
    replay_order_preview.add_argument("--sizing-json", type=Path, required=True, help="M111 account-scaled sizing JSON path")
    replay_order_preview.add_argument("--max-preview-items", type=int, default=25)
    replay_order_preview.add_argument("--exclude-human-review", action="store_true", help="Block items that still require human review")
    replay_order_preview.add_argument("--sample-limit", type=int, default=20)
    replay_order_preview.add_argument("--output", type=Path, help="Optional Markdown order-preview report path")
    replay_order_preview.add_argument("--json-output-path", type=Path, help="Optional JSON order-preview report path")
    replay_order_preview.add_argument("--json", action="store_true", help="Print machine-readable order-preview JSON")
    replay_auto_execution_gate = sub.add_parser(
        "replay-score-auto-execution-gate",
        help="Convert paper order-preview review reasons into deterministic autonomous-execution hard gates",
    )
    replay_auto_execution_gate.add_argument("--preview-json", type=Path, required=True, help="M112 local order-preview JSON path")
    replay_auto_execution_gate.add_argument("--broker-whatif-json", type=Path, help="Optional IBKR what-if JSON for broker-side margin gate")
    replay_auto_execution_gate.add_argument("--short-availability-json", type=Path, help="Optional IBKR short availability JSON for short-stock gate")
    replay_auto_execution_gate.add_argument("--evidence-preflight-json", type=Path, help="Optional M155 evidence preflight JSON for freshness/recovery-aware paper gating")
    replay_auto_execution_gate.add_argument("--market-data-risk-gate-json", type=Path, help="Optional M174 market-data risk gate JSON for market-quality/temperature-aware paper gating")
    replay_auto_execution_gate.add_argument("--allow-fractional-shares", action="store_true")
    replay_auto_execution_gate.add_argument("--allow-short-stock", action="store_true")
    replay_auto_execution_gate.add_argument("--no-require-short-availability", action="store_true", help="Do not require IBKR short availability evidence for short-stock items")
    replay_auto_execution_gate.add_argument("--require-broker-whatif", action="store_true")
    replay_auto_execution_gate.add_argument("--max-auto-orders", type=int, default=10)
    replay_auto_execution_gate.add_argument("--min-cash-buffer", type=float, default=0.0)
    replay_auto_execution_gate.add_argument("--max-local-initial-margin-total", type=float)
    replay_auto_execution_gate.add_argument("--allowed-ticket-types", default="stock")
    replay_auto_execution_gate.add_argument("--allowed-actions", default="BUY,SELL")
    replay_auto_execution_gate.add_argument("--sample-limit", type=int, default=20)
    replay_auto_execution_gate.add_argument("--output", type=Path, help="Optional Markdown autonomous execution gate report path")
    replay_auto_execution_gate.add_argument("--json-output-path", type=Path, help="Optional JSON autonomous execution gate report path")
    replay_auto_execution_gate.add_argument("--json", action="store_true", help="Print machine-readable autonomous execution gate JSON")
    replay_capital_aware_action_gate = sub.add_parser(
        "replay-score-capital-aware-action-gate",
        help="Apply M238 capital/margin budgets to an M113/M182 paper auto gate before intent ledgering",
    )
    replay_capital_aware_action_gate.add_argument("--auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"), help="M113/M182 auto-gate JSON path")
    replay_capital_aware_action_gate.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"), help="M237/M238 capital-margin state JSON path")
    replay_capital_aware_action_gate.add_argument("--no-reduce-to-fit-capacity", action="store_true", help="Block oversized intents instead of reducing paper quantity to fit capital budgets")
    replay_capital_aware_action_gate.add_argument("--allow-fractional-shares", action="store_true", help="Allow M239 to reduce stock quantities fractionally when the source intent did not already use fractional shares")
    replay_capital_aware_action_gate.add_argument("--min-stock-quantity", type=float, default=0.01)
    replay_capital_aware_action_gate.add_argument("--stock-quantity-increment", type=float, default=0.01)
    replay_capital_aware_action_gate.add_argument("--max-ready-actions", type=int, default=10)
    replay_capital_aware_action_gate.add_argument("--sample-limit", type=int, default=20)
    replay_capital_aware_action_gate.add_argument("--output", type=Path, default=Path("exports/m239_capital_aware_action_gate.md"), help="Optional Markdown M239 report path")
    replay_capital_aware_action_gate.add_argument("--json-output-path", type=Path, default=Path("exports/m239_capital_aware_action_gate.json"), help="Optional JSON M239 report path")
    replay_capital_aware_action_gate.add_argument("--json", action="store_true", help="Print machine-readable M239 capital-aware action gate JSON")
    market_data_operating_auto_gate = sub.add_parser(
        "market-data-operating-auto-gate",
        help="Apply M181 market-data defensive operating mode to an M113 paper auto gate",
    )
    market_data_operating_auto_gate.add_argument("--auto-gate-json", type=Path, default=Path("exports/m175_market_data_risk_auto_gate_latest.json"))
    market_data_operating_auto_gate.add_argument("--defensive-mode-json", type=Path, default=Path("exports/m181_market_data_defensive_mode_latest.json"))
    market_data_operating_auto_gate.add_argument("--sample-limit", type=int, default=20)
    market_data_operating_auto_gate.add_argument("--output", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.md"), help="Optional Markdown M182 operating auto-gate report path")
    market_data_operating_auto_gate.add_argument("--json-output-path", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"), help="Optional JSON M182 operating auto-gate report path")
    market_data_operating_auto_gate.add_argument("--json", action="store_true", help="Print machine-readable M182 operating auto-gate JSON")
    add_operating_defensive_safety_commands(sub)
    replay_execution_intent_ledger = sub.add_parser(
        "replay-score-execution-intent-ledger",
        help="Create a deduplicated paper execution-intent ledger from capital-aware autonomous gate output, optionally gated by M123 portfolio risk state",
    )
    replay_execution_intent_ledger.add_argument(
        "--auto-gate-json",
        type=Path,
        default=Path("exports/m239_capital_aware_action_gate.json"),
        help="M239 capital-aware action gate JSON path; legacy M113/M182 autonomous gate JSON is still accepted when explicitly provided",
    )
    replay_execution_intent_ledger.add_argument("--existing-ledger-json", type=Path, help="Existing M114 ledger JSON for duplicate detection")
    replay_execution_intent_ledger.add_argument("--portfolio-risk-state-json", type=Path, help="Optional M123 paper portfolio risk-state JSON path")
    replay_execution_intent_ledger.add_argument("--session-transition-release-governor-json", type=Path, default=Path("exports/m201_session_transition_release_governor.json"), help="Optional M201 release governor JSON; when present it gates ordinary alpha paper intents")
    replay_execution_intent_ledger.add_argument("--require-capital-aware-gate", action="store_true", help="Require M239 capital-aware gate input before creating pending paper execution intents")
    replay_execution_intent_ledger.add_argument("--monitor-size-multiplier", type=float, default=0.50, help="Position-size multiplier when M123 risk_state is monitor")
    replay_execution_intent_ledger.add_argument("--max-intents", type=int, default=10)
    replay_execution_intent_ledger.add_argument("--ledger-run-id", help="Optional stable ledger run identifier")
    replay_execution_intent_ledger.add_argument("--sample-limit", type=int, default=20)
    replay_execution_intent_ledger.add_argument("--output", type=Path, help="Optional Markdown execution-intent ledger path")
    replay_execution_intent_ledger.add_argument("--json-output-path", type=Path, help="Optional JSON execution-intent ledger path")
    replay_execution_intent_ledger.add_argument("--json", action="store_true", help="Print machine-readable execution-intent ledger JSON")
    replay_paper_capital_aware_execution = sub.add_parser(
        "replay-score-paper-capital-aware-execution",
        help="Run M242 paper execution through the M239 capital-aware gate before ledgering and simulated routing",
    )
    replay_paper_capital_aware_execution.add_argument("--capital-aware-gate-json", type=Path, default=Path("exports/m239_capital_aware_action_gate.json"), help="M239 capital-aware action gate JSON path")
    replay_paper_capital_aware_execution.add_argument("--existing-ledger-json", type=Path, help="Existing M242/M124 ledger JSON for duplicate detection")
    replay_paper_capital_aware_execution.add_argument("--portfolio-risk-state-json", type=Path, default=Path("exports/m123_paper_portfolio_risk_state.json"), help="M123 paper portfolio risk-state JSON path; required by M242 router gate when pending intents exist")
    replay_paper_capital_aware_execution.add_argument("--session-transition-release-governor-json", type=Path, default=Path("exports/m206_session_transition_release_governor.json"), help="M206/M201 release governor JSON path; required by M242 router gate when pending ordinary-alpha intents exist")
    replay_paper_capital_aware_execution.add_argument("--existing-router-json", type=Path, help="Existing M242/M115 router JSON for duplicate routed-intent detection")
    replay_paper_capital_aware_execution.add_argument("--marks-json", type=Path, help="Optional mark snapshot JSON to use for paper fill reference prices")
    replay_paper_capital_aware_execution.add_argument("--operating-auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"), help="M182 operating auto-gate JSON used as final preflight before simulated routing")
    replay_paper_capital_aware_execution.add_argument("--monitor-size-multiplier", type=float, default=0.50)
    replay_paper_capital_aware_execution.add_argument("--max-intents", type=int, default=10)
    replay_paper_capital_aware_execution.add_argument("--max-orders", type=int, default=10)
    replay_paper_capital_aware_execution.add_argument("--fill-price-source", choices=("local_estimate", "marks_or_local_estimate", "require_marks"), default="local_estimate")
    replay_paper_capital_aware_execution.add_argument("--fill-model", default="immediate_local_estimate_fill_v1")
    replay_paper_capital_aware_execution.add_argument("--slippage-bps", type=float, default=0.0)
    replay_paper_capital_aware_execution.add_argument("--commission-per-order", type=float, default=0.0)
    replay_paper_capital_aware_execution.add_argument("--ledger-run-id")
    replay_paper_capital_aware_execution.add_argument("--router-run-id")
    replay_paper_capital_aware_execution.add_argument("--sample-limit", type=int, default=20)
    replay_paper_capital_aware_execution.add_argument("--output", type=Path, default=Path("exports/m242_paper_capital_aware_execution.md"), help="Optional Markdown M242 summary path")
    replay_paper_capital_aware_execution.add_argument("--json-output-path", type=Path, default=Path("exports/m242_paper_capital_aware_execution.json"), help="Optional JSON M242 summary path")
    replay_paper_capital_aware_execution.add_argument("--ledger-output", type=Path, default=Path("exports/m242_capital_aware_execution_intent_ledger.md"), help="Optional Markdown M242 ledger path")
    replay_paper_capital_aware_execution.add_argument("--ledger-json-output-path", type=Path, default=Path("exports/m242_capital_aware_execution_intent_ledger.json"), help="Optional JSON M242 ledger path")
    replay_paper_capital_aware_execution.add_argument("--router-output", type=Path, default=Path("exports/m242_capital_aware_paper_order_router.md"), help="Optional Markdown M242 router path")
    replay_paper_capital_aware_execution.add_argument("--router-json-output-path", type=Path, default=Path("exports/m242_capital_aware_paper_order_router.json"), help="Optional JSON M242 router path")
    replay_paper_capital_aware_execution.add_argument("--json", action="store_true", help="Print machine-readable M242 capital-aware paper execution JSON")
    replay_paper_order_router = sub.add_parser(
        "replay-score-paper-order-router",
        help="Consume pending paper execution intents and create simulated paper orders, fills, and positions",
    )
    replay_paper_order_router.add_argument("--intent-ledger-json", type=Path, required=True, help="M114 execution-intent ledger JSON path")
    replay_paper_order_router.add_argument("--existing-router-json", type=Path, help="Existing M115 paper router JSON for duplicate routed-intent detection")
    replay_paper_order_router.add_argument("--marks-json", type=Path, help="Optional M117 stock mark snapshot JSON to use for paper fill reference prices")
    replay_paper_order_router.add_argument("--operating-auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"), help="M182 operating auto-gate JSON used as final preflight before simulated routing")
    replay_paper_order_router.add_argument("--session-transition-release-governor-json", type=Path, default=Path("exports/m201_session_transition_release_governor.json"), help="Optional M201 release governor JSON used as a final ordinary-alpha release preflight")
    replay_paper_order_router.add_argument("--ignore-operating-preflight", action="store_true", help="Disable the M184 operating final preflight for legacy paper-router runs")
    replay_paper_order_router.add_argument("--allow-non-operating-source-ledger", action="store_true", help="Allow routing ledgers not derived from the M182 operating auto-gate")
    replay_paper_order_router.add_argument("--ignore-session-transition-release-governor", action="store_true", help="Disable the M204 session-transition release governor preflight for legacy paper-router runs")
    replay_paper_order_router.add_argument(
        "--fill-price-source",
        choices=("local_estimate", "marks_or_local_estimate", "require_marks"),
        default="local_estimate",
        help="Choose whether fills use local estimate prices, available marks with fallback, or require marks",
    )
    replay_paper_order_router.add_argument("--fill-model", default="immediate_local_estimate_fill_v1")
    replay_paper_order_router.add_argument("--require-capital-aware-gate", action="store_true", help="Require M242/M239 capital-aware ledger fields before simulated paper fills")
    replay_paper_order_router.add_argument("--require-portfolio-risk-gate", action="store_true", help="Require M124 portfolio-risk-gated pending intents before simulated paper fills")
    replay_paper_order_router.add_argument("--max-orders", type=int, default=10)
    replay_paper_order_router.add_argument("--slippage-bps", type=float, default=0.0)
    replay_paper_order_router.add_argument("--commission-per-order", type=float, default=0.0)
    replay_paper_order_router.add_argument("--router-run-id", help="Optional stable paper router run identifier")
    replay_paper_order_router.add_argument("--sample-limit", type=int, default=20)
    replay_paper_order_router.add_argument("--output", type=Path, help="Optional Markdown paper order-router report path")
    replay_paper_order_router.add_argument("--json-output-path", type=Path, help="Optional JSON paper order-router report path")
    replay_paper_order_router.add_argument("--json", action="store_true", help="Print machine-readable paper order-router JSON")
    replay_paper_position_mark = sub.add_parser(
        "replay-score-paper-position-mark",
        help="Mark M115 paper positions to market and emit paper exit signals",
    )
    replay_paper_position_mark.add_argument("--router-json", type=Path, required=True, help="M115 paper order-router JSON path")
    replay_paper_position_mark.add_argument("--marks-json", type=Path, help="Optional marks JSON with mark prices")
    replay_paper_position_mark.add_argument("--marks", help="Inline comma-separated marks, e.g. NFLX=104,TSLA=95")
    replay_paper_position_mark.add_argument("--take-profit-pct", type=float, default=0.03)
    replay_paper_position_mark.add_argument("--stop-loss-pct", type=float, default=0.02)
    replay_paper_position_mark.add_argument("--max-loss-dollars", type=float)
    replay_paper_position_mark.add_argument("--mark-run-id", help="Optional stable mark run identifier")
    replay_paper_position_mark.add_argument("--sample-limit", type=int, default=20)
    replay_paper_position_mark.add_argument("--output", type=Path, help="Optional Markdown paper position mark report path")
    replay_paper_position_mark.add_argument("--json-output-path", type=Path, help="Optional JSON paper position mark report path")
    replay_paper_position_mark.add_argument("--json", action="store_true", help="Print machine-readable paper position mark JSON")
    replay_paper_position_mark_timeseries = sub.add_parser(
        "replay-score-paper-position-mark-timeseries",
        help="Build a paper mark-to-market time-series ledger from repeated mark snapshots",
    )
    replay_paper_position_mark_timeseries.add_argument("--router-json", type=Path, required=True, help="M118/M115 paper order-router JSON path")
    replay_paper_position_mark_timeseries.add_argument("--marks-jsons", required=True, help="Comma-separated mark snapshot JSON paths in time order")
    replay_paper_position_mark_timeseries.add_argument("--take-profit-pct", type=float, default=0.03)
    replay_paper_position_mark_timeseries.add_argument("--stop-loss-pct", type=float, default=0.02)
    replay_paper_position_mark_timeseries.add_argument("--max-loss-dollars", type=float)
    replay_paper_position_mark_timeseries.add_argument("--series-run-id", help="Optional stable mark time-series run identifier")
    replay_paper_position_mark_timeseries.add_argument("--sample-limit", type=int, default=20)
    replay_paper_position_mark_timeseries.add_argument("--output", type=Path, help="Optional Markdown paper position mark time-series report path")
    replay_paper_position_mark_timeseries.add_argument("--json-output-path", type=Path, help="Optional JSON paper position mark time-series report path")
    replay_paper_position_mark_timeseries.add_argument("--json", action="store_true", help="Print machine-readable paper position mark time-series JSON")
    replay_paper_exit_intent_ledger = sub.add_parser(
        "replay-score-paper-exit-intent-ledger",
        help="Create a paper exit-intent ledger from latest paper mark-to-market exit signals",
    )
    replay_paper_exit_intent_ledger.add_argument("--timeseries-json", type=Path, required=True, help="M119 paper position mark time-series JSON path")
    replay_paper_exit_intent_ledger.add_argument("--existing-ledger-json", type=Path, help="Existing M120 paper exit-intent ledger JSON for duplicate detection")
    replay_paper_exit_intent_ledger.add_argument("--exit-signals", default="take_profit,stop_loss,max_loss_dollars")
    replay_paper_exit_intent_ledger.add_argument("--max-intents", type=int, default=10)
    replay_paper_exit_intent_ledger.add_argument("--ledger-run-id", help="Optional stable paper exit ledger run identifier")
    replay_paper_exit_intent_ledger.add_argument("--sample-limit", type=int, default=20)
    replay_paper_exit_intent_ledger.add_argument("--output", type=Path, help="Optional Markdown paper exit-intent ledger report path")
    replay_paper_exit_intent_ledger.add_argument("--json-output-path", type=Path, help="Optional JSON paper exit-intent ledger report path")
    replay_paper_exit_intent_ledger.add_argument("--json", action="store_true", help="Print machine-readable paper exit-intent ledger JSON")
    replay_paper_exit_router = sub.add_parser(
        "replay-score-paper-exit-router",
        help="Consume pending paper exit intents and create simulated paper close fills plus realized PnL rows",
    )
    replay_paper_exit_router.add_argument("--exit-ledger-json", type=Path, required=True, help="M120 paper exit-intent ledger JSON path")
    replay_paper_exit_router.add_argument("--existing-router-json", type=Path, help="Existing M121 paper exit-router JSON for duplicate routed-intent detection")
    replay_paper_exit_router.add_argument("--fill-model", default="immediate_mark_close_fill_v1")
    replay_paper_exit_router.add_argument("--max-exits", type=int, default=10)
    replay_paper_exit_router.add_argument("--slippage-bps", type=float, default=0.0)
    replay_paper_exit_router.add_argument("--commission-per-exit", type=float, default=0.0)
    replay_paper_exit_router.add_argument("--router-run-id", help="Optional stable paper exit router run identifier")
    replay_paper_exit_router.add_argument("--sample-limit", type=int, default=20)
    replay_paper_exit_router.add_argument("--output", type=Path, help="Optional Markdown paper exit-router report path")
    replay_paper_exit_router.add_argument("--json-output-path", type=Path, help="Optional JSON paper exit-router report path")
    replay_paper_exit_router.add_argument("--json", action="store_true", help="Print machine-readable paper exit-router JSON")
    replay_paper_portfolio_state = sub.add_parser(
        "replay-score-paper-portfolio-state",
        help="Reconcile paper entry fills, paper close fills, and latest marks into current paper portfolio state",
    )
    replay_paper_portfolio_state.add_argument("--entry-router-json", type=Path, required=True, help="M118/M115 paper entry router JSON path")
    replay_paper_portfolio_state.add_argument("--exit-router-json", type=Path, help="Optional M121 paper exit-router JSON path")
    replay_paper_portfolio_state.add_argument("--timeseries-json", type=Path, help="Optional M119 paper mark time-series JSON for latest marks")
    replay_paper_portfolio_state.add_argument("--state-run-id", help="Optional stable paper portfolio state run identifier")
    replay_paper_portfolio_state.add_argument("--sample-limit", type=int, default=20)
    replay_paper_portfolio_state.add_argument("--output", type=Path, help="Optional Markdown paper portfolio state report path")
    replay_paper_portfolio_state.add_argument("--json-output-path", type=Path, help="Optional JSON paper portfolio state report path")
    replay_paper_portfolio_state.add_argument("--json", action="store_true", help="Print machine-readable paper portfolio state JSON")
    replay_paper_portfolio_risk_state = sub.add_parser(
        "replay-score-paper-portfolio-risk-state",
        help="Evaluate current paper portfolio state against configurable paper risk limits",
    )
    replay_paper_portfolio_risk_state.add_argument("--portfolio-state-json", type=Path, required=True, help="M122 paper portfolio state JSON path")
    replay_paper_portfolio_risk_state.add_argument("--max-gross-exposure", type=float, default=100000.0)
    replay_paper_portfolio_risk_state.add_argument("--max-symbol-exposure", type=float, default=25000.0)
    replay_paper_portfolio_risk_state.add_argument("--max-short-exposure", type=float, default=25000.0)
    replay_paper_portfolio_risk_state.add_argument("--max-unrealized-loss", type=float, default=1000.0)
    replay_paper_portfolio_risk_state.add_argument("--max-total-loss", type=float, default=1000.0)
    replay_paper_portfolio_risk_state.add_argument("--max-open-symbols", type=int, default=25)
    replay_paper_portfolio_risk_state.add_argument("--warning-ratio", type=float, default=0.80)
    replay_paper_portfolio_risk_state.add_argument("--risk-run-id", help="Optional stable paper portfolio risk run identifier")
    replay_paper_portfolio_risk_state.add_argument("--sample-limit", type=int, default=20)
    replay_paper_portfolio_risk_state.add_argument("--output", type=Path, help="Optional Markdown paper portfolio risk report path")
    replay_paper_portfolio_risk_state.add_argument("--json-output-path", type=Path, help="Optional JSON paper portfolio risk report path")
    replay_paper_portfolio_risk_state.add_argument("--json", action="store_true", help="Print machine-readable paper portfolio risk JSON")
    replay_paper_portfolio_risk_loop = sub.add_parser(
        "replay-score-paper-portfolio-risk-loop",
        help="Run the M126 paper loop from required-gate router fills through marks, portfolio state, and next risk state",
    )
    replay_paper_portfolio_risk_loop.add_argument("--entry-router-json", type=Path, required=True, help="M125 required-gate paper entry router JSON path")
    replay_paper_portfolio_risk_loop.add_argument("--marks-jsons", required=True, help="Comma-separated mark snapshot JSON paths in time order")
    replay_paper_portfolio_risk_loop.add_argument("--exit-router-json", type=Path, help="Optional M121 paper exit-router JSON path")
    replay_paper_portfolio_risk_loop.add_argument("--take-profit-pct", type=float, default=0.03)
    replay_paper_portfolio_risk_loop.add_argument("--stop-loss-pct", type=float, default=0.02)
    replay_paper_portfolio_risk_loop.add_argument("--max-loss-dollars", type=float)
    replay_paper_portfolio_risk_loop.add_argument("--max-gross-exposure", type=float, default=100000.0)
    replay_paper_portfolio_risk_loop.add_argument("--max-symbol-exposure", type=float, default=25000.0)
    replay_paper_portfolio_risk_loop.add_argument("--max-short-exposure", type=float, default=25000.0)
    replay_paper_portfolio_risk_loop.add_argument("--max-unrealized-loss", type=float, default=1000.0)
    replay_paper_portfolio_risk_loop.add_argument("--max-total-loss", type=float, default=1000.0)
    replay_paper_portfolio_risk_loop.add_argument("--max-open-symbols", type=int, default=25)
    replay_paper_portfolio_risk_loop.add_argument("--warning-ratio", type=float, default=0.80)
    replay_paper_portfolio_risk_loop.add_argument("--monitor-size-multiplier", type=float, default=0.50)
    replay_paper_portfolio_risk_loop.add_argument("--loop-run-id", help="Optional stable paper portfolio risk-loop run identifier")
    replay_paper_portfolio_risk_loop.add_argument("--sample-limit", type=int, default=20)
    replay_paper_portfolio_risk_loop.add_argument("--output", type=Path, help="Optional Markdown paper portfolio risk-loop report path")
    replay_paper_portfolio_risk_loop.add_argument("--json-output-path", type=Path, help="Optional combined JSON paper portfolio risk-loop report path")
    replay_paper_portfolio_risk_loop.add_argument("--timeseries-json-output-path", type=Path, help="Optional M119-compatible component JSON output path")
    replay_paper_portfolio_risk_loop.add_argument("--portfolio-state-json-output-path", type=Path, help="Optional M122-compatible component JSON output path")
    replay_paper_portfolio_risk_loop.add_argument("--portfolio-risk-json-output-path", type=Path, help="Optional M123-compatible component JSON output path for the next M124 cycle")
    replay_paper_portfolio_risk_loop.add_argument("--json", action="store_true", help="Print machine-readable paper portfolio risk-loop JSON")
