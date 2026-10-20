from __future__ import annotations

from pathlib import Path


def add_strategy_allocation_commands(sub) -> None:
    ticker_candidate_ranking = sub.add_parser(
        "ticker-candidate-ranking",
        help="M241.5: rank ticker candidates from news/events before M241 strategy expression selection",
    )
    ticker_candidate_ranking.add_argument("--database-path", type=Path)
    ticker_candidate_ranking.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    ticker_candidate_ranking.add_argument("--options-market-structure-json", type=Path, default=Path("exports/m217_options_market_structure_desk.json"))
    ticker_candidate_ranking.add_argument(
        "--options-market-structure-supervisor-json",
        type=Path,
        default=Path("exports/m219_options_market_structure_supervisor.json"),
    )
    ticker_candidate_ranking.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    ticker_candidate_ranking.add_argument("--news-learning-json", type=Path, default=Path("exports/m236_news_learning_report.json"))
    ticker_candidate_ranking.add_argument("--lookback-hours", type=float, default=24.0)
    ticker_candidate_ranking.add_argument("--recency-half-life-hours", type=float, default=6.0)
    ticker_candidate_ranking.add_argument("--max-news-events", type=int, default=250)
    ticker_candidate_ranking.add_argument("--max-ranked-candidates", type=int, default=50)
    ticker_candidate_ranking.add_argument("--min-total-score", type=float, default=0.0)
    ticker_candidate_ranking.add_argument("--output", type=Path, default=Path("exports/m241_5_ticker_candidate_ranking.md"))
    ticker_candidate_ranking.add_argument("--json-output-path", type=Path, default=Path("exports/m241_5_ticker_candidate_ranking.json"))
    ticker_candidate_ranking.add_argument("--json", action="store_true", help="Print machine-readable M241.5 ticker candidate ranking JSON")

    cross_asset_expression_ranking = sub.add_parser(
        "cross-asset-expression-ranking",
        help="M241.7: rank efficient cross-asset expressions after scorer/ticker direction and before M241.6 candidate synthesis",
    )
    cross_asset_expression_ranking.add_argument("--ticker-candidate-ranking-json", type=Path, default=Path("exports/m241_5_ticker_candidate_ranking.json"))
    cross_asset_expression_ranking.add_argument("--database-path", type=Path)
    cross_asset_expression_ranking.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    cross_asset_expression_ranking.add_argument("--options-market-structure-json", type=Path, default=Path("exports/m217_options_market_structure_desk.json"))
    cross_asset_expression_ranking.add_argument(
        "--options-market-structure-supervisor-json",
        type=Path,
        default=Path("exports/m219_options_market_structure_supervisor.json"),
    )
    cross_asset_expression_ranking.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    cross_asset_expression_ranking.add_argument("--cross-asset-tuning-json", type=Path, default=Path("exports/m241_8_cross_asset_expression_outcomes.json"))
    cross_asset_expression_ranking.add_argument("--lookback-hours", type=float, default=24.0)
    cross_asset_expression_ranking.add_argument("--max-scorer-predictions", type=int, default=250)
    cross_asset_expression_ranking.add_argument("--max-expressions", type=int, default=30)
    cross_asset_expression_ranking.add_argument("--min-expression-score", type=float, default=35.0)
    cross_asset_expression_ranking.add_argument("--output", type=Path, default=Path("exports/m241_7_cross_asset_expression_ranking.md"))
    cross_asset_expression_ranking.add_argument("--json-output-path", type=Path, default=Path("exports/m241_7_cross_asset_expression_ranking.json"))
    cross_asset_expression_ranking.add_argument("--json", action="store_true", help="Print machine-readable M241.7 cross-asset expression ranking JSON")

    cross_asset_expression_outcomes = sub.add_parser(
        "cross-asset-expression-outcomes",
        help="M241.8: record/evaluate M241.7 cross-asset expression outcomes and emit score-tuning adjustments",
    )
    cross_asset_expression_outcomes.add_argument("--cross-asset-expression-ranking-json", type=Path, default=Path("exports/m241_7_cross_asset_expression_ranking.json"))
    cross_asset_expression_outcomes.add_argument("--database-path", type=Path)
    cross_asset_expression_outcomes.add_argument("--horizons-minutes", default="5,15,30")
    cross_asset_expression_outcomes.add_argument("--max-expressions-to-record", type=int, default=30)
    cross_asset_expression_outcomes.add_argument("--flat-threshold-pct", type=float, default=0.05)
    cross_asset_expression_outcomes.add_argument("--max-baseline-age-minutes", type=float, default=390.0)
    cross_asset_expression_outcomes.add_argument("--max-outcome-lag-minutes", type=float, default=1440.0)
    cross_asset_expression_outcomes.add_argument("--min-evaluated-samples", type=int, default=3)
    cross_asset_expression_outcomes.add_argument("--adjustment-cap", type=float, default=12.0)
    cross_asset_expression_outcomes.add_argument("--evaluation-limit", type=int, default=2000)
    cross_asset_expression_outcomes.add_argument("--no-record-current-snapshot", action="store_true")
    cross_asset_expression_outcomes.add_argument("--no-evaluate-pending", action="store_true")
    cross_asset_expression_outcomes.add_argument("--output", type=Path, default=Path("exports/m241_8_cross_asset_expression_outcomes.md"))
    cross_asset_expression_outcomes.add_argument("--json-output-path", type=Path, default=Path("exports/m241_8_cross_asset_expression_outcomes.json"))
    cross_asset_expression_outcomes.add_argument("--json", action="store_true", help="Print machine-readable M241.8 cross-asset expression outcomes JSON")

    ticker_action_candidates = sub.add_parser(
        "ticker-action-candidates",
        help="M241.6: synthesize paper/read-only M241 strategy-review candidates from M241.5 ranked tickers",
    )
    ticker_action_candidates.add_argument("--ticker-candidate-ranking-json", type=Path, default=Path("exports/m241_5_ticker_candidate_ranking.json"))
    ticker_action_candidates.add_argument("--cross-asset-expression-ranking-json", type=Path, default=Path("exports/m241_7_cross_asset_expression_ranking.json"))
    ticker_action_candidates.add_argument("--capital-aware-gate-json", type=Path, default=Path("exports/m239_capital_aware_action_gate.json"))
    ticker_action_candidates.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    ticker_action_candidates.add_argument("--max-rank", type=int, default=20)
    ticker_action_candidates.add_argument("--max-synthetic-candidates", type=int, default=12)
    ticker_action_candidates.add_argument("--min-total-score", type=float, default=45.0)
    ticker_action_candidates.add_argument("--min-confidence", choices=("low", "medium", "high"), default="medium")
    ticker_action_candidates.add_argument("--include-existing-symbols", action="store_true")
    ticker_action_candidates.add_argument("--allow-neutral-watch-injection", action="store_true")
    ticker_action_candidates.add_argument("--default-stock-notional", type=float, default=25.0)
    ticker_action_candidates.add_argument("--min-fractional-stock-cash", type=float, default=10.0)
    ticker_action_candidates.add_argument("--default-option-probe-premium", type=float, default=25.0)
    ticker_action_candidates.add_argument("--output", type=Path, default=Path("exports/m241_6_ticker_action_candidates.md"))
    ticker_action_candidates.add_argument("--json-output-path", type=Path, default=Path("exports/m241_6_ticker_action_candidates.json"))
    ticker_action_candidates.add_argument("--json", action="store_true", help="Print machine-readable M241.6 ticker action candidates JSON")

    strategy_allocation = sub.add_parser(
        "strategy-allocation-engine",
        help="Build M241 paper/read-only strategy allocation recommendations from scorer/news/options/market/capital context",
    )
    strategy_allocation.add_argument("--capital-aware-gate-json", type=Path, default=Path("exports/m239_capital_aware_action_gate.json"))
    strategy_allocation.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    strategy_allocation.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    strategy_allocation.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m175_dashboard_market_data_risk_gate_latest.json"))
    strategy_allocation.add_argument("--options-market-structure-json", type=Path, default=Path("exports/m217_options_market_structure_desk.json"))
    strategy_allocation.add_argument(
        "--options-market-structure-supervisor-json",
        type=Path,
        default=Path("exports/m219_options_market_structure_supervisor.json"),
    )
    strategy_allocation.add_argument("--live-scorer-learning-json", type=Path, default=Path("exports/m223_live_scorer_learning.json"))
    strategy_allocation.add_argument("--news-learning-json", type=Path, default=Path("exports/m236_news_learning_report.json"))
    strategy_allocation.add_argument("--ticker-candidate-ranking-json", type=Path, default=Path("exports/m241_5_ticker_candidate_ranking.json"))
    strategy_allocation.add_argument("--ticker-action-candidates-json", type=Path, default=Path("exports/m241_6_ticker_action_candidates.json"))
    strategy_allocation.add_argument("--micro-policy-state-json", type=Path, default=Path("exports/m246_micro_cash_policy_state.json"))
    strategy_allocation.add_argument("--database-path", type=Path)
    strategy_allocation.add_argument("--min-option-budget", type=float, default=100.0)
    strategy_allocation.add_argument("--min-spread-budget", type=float, default=150.0)
    strategy_allocation.add_argument("--max-recommendations", type=int, default=25)
    strategy_allocation.add_argument("--latest-news-limit", type=int, default=12)
    strategy_allocation.add_argument("--output", type=Path, default=Path("exports/m241_strategy_allocation_engine.md"))
    strategy_allocation.add_argument("--json-output-path", type=Path, default=Path("exports/m241_strategy_allocation_engine.json"))
    strategy_allocation.add_argument("--json", action="store_true", help="Print machine-readable M241 strategy allocation JSON")

    micro_option_candidates = sub.add_parser(
        "micro-option-candidates",
        help="M247: build read-only micro-cash option contract candidates from stored option quotes and capital budgets",
    )
    micro_option_candidates.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    micro_option_candidates.add_argument("--strategy-allocation-json", type=Path, default=Path("exports/m241_strategy_allocation_engine.json"))
    micro_option_candidates.add_argument("--option-quote-json", type=Path, default=Path("exports/m218_fresh_option_quote_snapshot.json"))
    micro_option_candidates.add_argument("--options-market-structure-json", type=Path, default=Path("exports/m217_options_market_structure_desk.json"))
    micro_option_candidates.add_argument(
        "--options-market-structure-supervisor-json",
        type=Path,
        default=Path("exports/m219_options_market_structure_supervisor.json"),
    )
    micro_option_candidates.add_argument("--micro-policy-state-json", type=Path, default=Path("exports/m246_micro_cash_policy_state.json"))
    micro_option_candidates.add_argument("--symbols", default="SPY,QQQ,IWM,DIA,NVDA,TSLA,AAPL")
    micro_option_candidates.add_argument("--min-option-budget", type=float, default=100.0)
    micro_option_candidates.add_argument("--min-spread-budget", type=float, default=150.0)
    micro_option_candidates.add_argument("--max-quote-age-minutes", type=float, default=30.0)
    micro_option_candidates.add_argument("--max-spread-pct", type=float, default=0.30)
    micro_option_candidates.add_argument("--min-volume", type=float, default=1.0)
    micro_option_candidates.add_argument("--min-open-interest", type=float, default=0.0)
    micro_option_candidates.add_argument("--min-dte", type=int, default=0)
    micro_option_candidates.add_argument("--max-dte", type=int, default=21)
    micro_option_candidates.add_argument("--max-premium-budget-fraction", type=float, default=1.0)
    micro_option_candidates.add_argument("--max-candidates", type=int, default=25)
    micro_option_candidates.add_argument("--output", type=Path, default=Path("exports/m247_micro_option_candidates.md"))
    micro_option_candidates.add_argument("--json-output-path", type=Path, default=Path("exports/m247_micro_option_candidates.json"))
    micro_option_candidates.add_argument("--json", action="store_true", help="Print machine-readable M247 micro option candidate JSON")

    paper_option_ticket_preview = sub.add_parser(
        "paper-option-ticket-preview",
        help="M248: convert M247 ready option candidates into paper-only option ticket previews; no broker order or what-if request",
    )
    paper_option_ticket_preview.add_argument("--micro-option-candidates-json", type=Path, default=Path("exports/m247_micro_option_candidates.json"))
    paper_option_ticket_preview.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    paper_option_ticket_preview.add_argument("--max-tickets", type=int, default=10)
    paper_option_ticket_preview.add_argument("--default-quantity", type=int, default=1)
    paper_option_ticket_preview.add_argument(
        "--limit-price-policy",
        choices=("conservative_debit", "mid_to_ask", "midpoint"),
        default="conservative_debit",
    )
    paper_option_ticket_preview.add_argument("--allow-stale-quotes", action="store_true", help="Allow stale M247 quote previews into blocked review only; ready tickets still require M247 readiness")
    paper_option_ticket_preview.add_argument("--max-ticket-debit", type=float, default=0.0, help="Optional per-ticket debit cap; 0 disables this extra cap")
    paper_option_ticket_preview.add_argument("--output", type=Path, default=Path("exports/m248_paper_option_ticket_preview.md"))
    paper_option_ticket_preview.add_argument("--json-output-path", type=Path, default=Path("exports/m248_paper_option_ticket_preview.json"))
    paper_option_ticket_preview.add_argument("--json", action="store_true", help="Print machine-readable M248 paper option ticket preview JSON")

    option_whatif_safety = sub.add_parser(
        "option-whatif-safety-validation",
        help="M249: validate M248 option tickets against local option safety, capital budgets, and optional explicit IBKR what-if evidence",
    )
    option_whatif_safety.add_argument("--paper-option-ticket-preview-json", type=Path, default=Path("exports/m248_paper_option_ticket_preview.json"))
    option_whatif_safety.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    option_whatif_safety.add_argument("--option-quote-json", type=Path, default=Path("exports/m218_fresh_option_quote_snapshot.json"))
    option_whatif_safety.add_argument(
        "--options-market-structure-supervisor-json",
        type=Path,
        default=Path("exports/m219_options_market_structure_supervisor.json"),
    )
    option_whatif_safety.add_argument("--broker-whatif-json", type=Path, default=Path("exports/m249_option_whatif_refresh.json"))
    option_whatif_safety.add_argument("--allow-missing-broker-whatif", action="store_true", help="Allow local-safety pass to remain whatif_pending instead of blocking when broker what-if is missing")
    option_whatif_safety.add_argument("--execute-whatif-refresh", action="store_true", help="Explicitly request IBKR option what-if with whatIf=True; no live order is routed")
    option_whatif_safety.add_argument("--max-whatif-tickets", type=int, default=3)
    option_whatif_safety.add_argument("--whatif-timeout-seconds", type=float, default=20.0)
    option_whatif_safety.add_argument("--whatif-client-id", type=int)
    option_whatif_safety.add_argument("--max-spread-pct", type=float, default=0.30)
    option_whatif_safety.add_argument("--max-quote-age-minutes", type=float, default=30.0)
    option_whatif_safety.add_argument("--allow-stale-quotes", action="store_true", help="Do not require quote snapshot freshness in the local safety check")
    option_whatif_safety.add_argument("--output", type=Path, default=Path("exports/m249_option_whatif_safety_validation.md"))
    option_whatif_safety.add_argument("--json-output-path", type=Path, default=Path("exports/m249_option_whatif_safety_validation.json"))
    option_whatif_safety.add_argument("--json", action="store_true", help="Print machine-readable M249 option what-if safety validation JSON")

    limited_auto_trading_readiness = sub.add_parser(
        "limited-auto-trading-readiness",
        help="M250: final read-only/paper guard that decides whether the stack is eligible for limited auto-trading readiness; live routing stays locked",
    )
    limited_auto_trading_readiness.add_argument("--capital-state-json", type=Path, default=Path("exports/m237_capital_margin_state.json"))
    limited_auto_trading_readiness.add_argument("--capital-pipeline-orchestrator-json", type=Path, default=Path("exports/m244_capital_pipeline_orchestrator.json"))
    limited_auto_trading_readiness.add_argument("--strategy-allocation-json", type=Path, default=Path("exports/m241_strategy_allocation_engine.json"))
    limited_auto_trading_readiness.add_argument("--live-read-only-capital-validation-json", type=Path, default=Path("exports/m243_live_read_only_capital_validation.json"))
    limited_auto_trading_readiness.add_argument("--micro-option-candidates-json", type=Path, default=Path("exports/m247_micro_option_candidates.json"))
    limited_auto_trading_readiness.add_argument("--paper-option-ticket-preview-json", type=Path, default=Path("exports/m248_paper_option_ticket_preview.json"))
    limited_auto_trading_readiness.add_argument("--option-whatif-safety-validation-json", type=Path, default=Path("exports/m249_option_whatif_safety_validation.json"))
    limited_auto_trading_readiness.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    limited_auto_trading_readiness.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m175_dashboard_market_data_risk_gate_latest.json"))
    limited_auto_trading_readiness.add_argument(
        "--options-market-structure-supervisor-json",
        type=Path,
        default=Path("exports/m219_options_market_structure_supervisor.json"),
    )
    limited_auto_trading_readiness.add_argument("--live-scorer-learning-json", type=Path, default=Path("exports/m223_live_scorer_learning.json"))
    limited_auto_trading_readiness.add_argument("--threshold-candidate-evaluator-json", type=Path, default=Path("exports/m226_threshold_candidate_evaluator_summary.json"))
    limited_auto_trading_readiness.add_argument("--evidence-freshness-json", type=Path, default=Path("exports/m152_evidence_freshness_desk.json"))
    limited_auto_trading_readiness.add_argument("--source-online-supervisor-json", type=Path, default=Path("exports/m173_source_online_supervisor.json"))
    limited_auto_trading_readiness.add_argument("--execution-safety-cache-supervisor-json", type=Path, default=Path("exports/m211_execution_safety_cache_supervisor_after_refresh.json"))
    limited_auto_trading_readiness.add_argument("--max-artifact-age-minutes", type=float, default=30.0)
    limited_auto_trading_readiness.add_argument("--output", type=Path, default=Path("exports/m250_limited_auto_trading_readiness.md"))
    limited_auto_trading_readiness.add_argument("--json-output-path", type=Path, default=Path("exports/m250_limited_auto_trading_readiness.json"))
    limited_auto_trading_readiness.add_argument("--json", action="store_true", help="Print machine-readable M250 limited auto-trading readiness JSON")

    gate_severity_outcomes = sub.add_parser(
        "gate-severity-outcomes",
        help="M250.5: record/evaluate gate severity posture, size/confidence multipliers, and paper outcome rows",
    )
    gate_severity_outcomes.add_argument("--database-path", type=Path)
    gate_severity_outcomes.add_argument("--limited-auto-readiness-json", type=Path, default=Path("exports/m250_limited_auto_trading_readiness.json"))
    gate_severity_outcomes.add_argument("--paper-capital-execution-json", type=Path, default=Path("exports/m242_paper_capital_aware_execution.json"))
    gate_severity_outcomes.add_argument("--horizons-minutes", default="5,15,30")
    gate_severity_outcomes.add_argument("--max-outcome-lag-minutes", type=float, default=1440.0)
    gate_severity_outcomes.add_argument("--evaluation-limit", type=int, default=2000)
    gate_severity_outcomes.add_argument("--no-record-current-snapshot", action="store_true")
    gate_severity_outcomes.add_argument("--no-evaluate-pending", action="store_true")
    gate_severity_outcomes.add_argument("--output", type=Path, default=Path("exports/m250_5_gate_severity_outcomes.md"))
    gate_severity_outcomes.add_argument("--json-output-path", type=Path, default=Path("exports/m250_5_gate_severity_outcomes.json"))
    gate_severity_outcomes.add_argument("--json", action="store_true", help="Print machine-readable M250.5 gate severity outcome JSON")
