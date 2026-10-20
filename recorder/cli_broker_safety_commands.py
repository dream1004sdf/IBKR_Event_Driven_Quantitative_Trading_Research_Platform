from __future__ import annotations

from pathlib import Path
from typing import Any


def add_ibkr_broker_evidence_commands(sub: Any) -> None:
    """Register read-only IBKR broker evidence commands."""

    ibkr_account_snapshot = sub.add_parser(
        "ibkr-account-snapshot",
        help="Read-only IBKR account summary, positions, and open-order snapshot; never submits orders",
    )
    ibkr_account_snapshot.add_argument("--account", help="Optional IBKR account ID filter")
    ibkr_account_snapshot.add_argument(
        "--tags",
        default="NetLiquidation,TotalCashValue,AvailableFunds,BuyingPower,ExcessLiquidity,MaintMarginReq,InitMarginReq",
        help="Comma-separated IBKR account summary tags",
    )
    ibkr_account_snapshot.add_argument("--timeout-seconds", type=float, default=12.0)
    ibkr_account_snapshot.add_argument("--no-positions", action="store_true", help="Skip reqPositions")
    ibkr_account_snapshot.add_argument("--no-open-orders", action="store_true", help="Skip reqOpenOrders")
    ibkr_account_snapshot.add_argument("--client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 8000")
    ibkr_account_snapshot.add_argument("--output", type=Path, help="Optional Markdown account snapshot report path")
    ibkr_account_snapshot.add_argument("--json-output-path", type=Path, help="Optional JSON account snapshot report path")
    ibkr_account_snapshot.add_argument("--json", action="store_true", help="Print machine-readable account snapshot JSON")

    ibkr_order_whatif = sub.add_parser(
        "ibkr-order-whatif-preview",
        help="Request IBKR broker-side margin preview with whatIf=True from a paper order-preview JSON; IBKR requires API transmit=True for what-if calculation but no live order is routed",
    )
    ibkr_order_whatif.add_argument("--preview-json", type=Path, required=True, help="M112 local order-preview JSON path")
    ibkr_order_whatif.add_argument("--max-orders", type=int, default=3)
    ibkr_order_whatif.add_argument("--include-human-review", action="store_true", help="Include preview_needs_human_review items")
    ibkr_order_whatif.add_argument("--allow-fractional", action="store_true", help="Allow fractional quantity what-if requests")
    ibkr_order_whatif.add_argument("--timeout-seconds", type=float, default=20.0)
    ibkr_order_whatif.add_argument("--client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 9000")
    ibkr_order_whatif.add_argument("--output", type=Path, help="Optional Markdown IBKR what-if report path")
    ibkr_order_whatif.add_argument("--json-output-path", type=Path, help="Optional JSON IBKR what-if report path")
    ibkr_order_whatif.add_argument("--json", action="store_true", help="Print machine-readable IBKR what-if JSON")

    ibkr_short_availability = sub.add_parser(
        "ibkr-short-availability",
        help="Read-only IBKR shortable/shortable-shares snapshot using generic tick 236",
    )
    ibkr_short_availability.add_argument("--symbols", required=True, help="Comma-separated stock tickers")
    ibkr_short_availability.add_argument("--timeout-seconds", type=float, default=15.0)
    ibkr_short_availability.add_argument("--market-data-type", type=int, default=1, choices=(1, 2, 3, 4))
    ibkr_short_availability.add_argument("--client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 9100")
    ibkr_short_availability.add_argument("--output", type=Path, help="Optional Markdown short availability report path")
    ibkr_short_availability.add_argument("--json-output-path", type=Path, help="Optional JSON short availability report path")
    ibkr_short_availability.add_argument("--json", action="store_true", help="Print machine-readable short availability JSON")

    ibkr_stock_mark_snapshot = sub.add_parser(
        "ibkr-stock-mark-snapshot",
        help="Create a read-only IBKR stock mark snapshot for M116 paper position marking",
    )
    ibkr_stock_mark_snapshot.add_argument("--symbols", help="Comma-separated stock symbols, e.g. ORCL,NFLX,TSLA")
    ibkr_stock_mark_snapshot.add_argument("--router-json", type=Path, help="Optional M115 paper router JSON to derive position symbols")
    ibkr_stock_mark_snapshot.add_argument("--quote-seconds", type=float, default=8.0)
    ibkr_stock_mark_snapshot.add_argument("--market-data-type", type=int, default=1, choices=(1, 2, 3, 4))
    ibkr_stock_mark_snapshot.add_argument("--client-id", type=int, help="Override IBKR clientId; defaults to IBKR_CLIENT_ID + 9300")
    ibkr_stock_mark_snapshot.add_argument("--output", type=Path, help="Optional Markdown stock mark snapshot report path")
    ibkr_stock_mark_snapshot.add_argument("--json-output-path", type=Path, help="Optional JSON stock mark snapshot report path")
    ibkr_stock_mark_snapshot.add_argument("--json", action="store_true", help="Print machine-readable stock mark snapshot JSON")


def add_operating_defensive_safety_commands(sub: Any) -> None:
    """Register paper-only operating defensive and safety-cache commands."""

    operating_defensive_plan = sub.add_parser(
        "operating-defensive-action-plan",
        help="Build an M186 paper-only defensive action plan from M182 operating state and portfolio rows",
    )
    operating_defensive_plan.add_argument("--operating-auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"))
    operating_defensive_plan.add_argument("--paper-portfolio-state-json", type=Path, default=Path("exports/m122_paper_portfolio_state.json"))
    operating_defensive_plan.add_argument("--account-positions-json", type=Path, help="Optional account/portfolio snapshot JSON for live-read-only position context")
    operating_defensive_plan.add_argument("--max-candidates", type=int, default=25)
    operating_defensive_plan.add_argument("--max-actions-per-symbol", type=int, default=4)
    operating_defensive_plan.add_argument("--hedge-notional-fraction", type=float, default=0.50)
    operating_defensive_plan.add_argument("--output", type=Path, default=Path("exports/m186_operating_defensive_action_plan.md"), help="Optional Markdown defensive action plan path")
    operating_defensive_plan.add_argument("--json-output-path", type=Path, default=Path("exports/m186_operating_defensive_action_plan.json"), help="Optional JSON defensive action plan path")
    operating_defensive_plan.add_argument("--json", action="store_true", help="Print machine-readable defensive action plan JSON")

    operating_defensive_intent_ledger = sub.add_parser(
        "operating-defensive-intent-ledger",
        help="Convert M186 defensive action recommendations into a gated M187 paper defensive intent ledger",
    )
    operating_defensive_intent_ledger.add_argument("--defensive-action-plan-json", type=Path, default=Path("exports/m186_operating_defensive_action_plan.json"))
    operating_defensive_intent_ledger.add_argument("--existing-ledger-json", type=Path, help="Optional existing M187 ledger JSON for duplicate detection")
    operating_defensive_intent_ledger.add_argument("--option-quote-gate-json", type=Path, help="Optional live option quote/chain gate JSON evidence")
    operating_defensive_intent_ledger.add_argument("--broker-whatif-json", type=Path, help="Optional IBKR margin/order what-if preview JSON evidence")
    operating_defensive_intent_ledger.add_argument("--allow-missing-whatif", action="store_true", help="Dry paper rehearsal mode: allow stock risk-reduction intents without broker what-if evidence")
    operating_defensive_intent_ledger.add_argument("--allow-missing-option-quotes", action="store_true", help="Dry paper rehearsal mode: allow option intents without live option quote/chain evidence")
    operating_defensive_intent_ledger.add_argument("--max-intents", type=int, default=10)
    operating_defensive_intent_ledger.add_argument("--ledger-run-id", help="Optional stable M187 ledger run identifier")
    operating_defensive_intent_ledger.add_argument("--sample-limit", type=int, default=20)
    operating_defensive_intent_ledger.add_argument("--output", type=Path, default=Path("exports/m187_operating_defensive_intent_ledger.md"), help="Optional Markdown defensive intent ledger path")
    operating_defensive_intent_ledger.add_argument("--json-output-path", type=Path, default=Path("exports/m187_operating_defensive_intent_ledger.json"), help="Optional JSON defensive intent ledger path")
    operating_defensive_intent_ledger.add_argument("--json", action="store_true", help="Print machine-readable defensive intent ledger JSON")

    operating_defensive_gate_evidence = sub.add_parser(
        "operating-defensive-gate-evidence",
        help="Build M188 what-if/order-preview and option quote/chain evidence adapters for the M187 defensive ledger",
    )
    operating_defensive_gate_evidence.add_argument("--defensive-action-plan-json", type=Path, default=Path("exports/m186_operating_defensive_action_plan.json"))
    operating_defensive_gate_evidence.add_argument("--broker-whatif-json", type=Path, help="Optional IBKR what-if preview JSON from ibkr-order-whatif-preview")
    operating_defensive_gate_evidence.add_argument("--option-quote-json", type=Path, help="Optional IBKR option quote snapshot JSON")
    operating_defensive_gate_evidence.add_argument("--option-chain-json", type=Path, help="Optional IBKR option chain scan JSON")
    operating_defensive_gate_evidence.add_argument("--account", help="Optional IBKR account to include in generated what-if order previews")
    operating_defensive_gate_evidence.add_argument("--max-preview-items", type=int, default=10)
    operating_defensive_gate_evidence.add_argument("--sample-limit", type=int, default=20)
    operating_defensive_gate_evidence.add_argument("--order-preview-json-output-path", type=Path, default=Path("exports/m188_operating_defensive_order_preview.json"), help="M188 M112-compatible local order-preview JSON for ibkr-order-whatif-preview")
    operating_defensive_gate_evidence.add_argument("--output", type=Path, default=Path("exports/m188_operating_defensive_gate_evidence.md"), help="Optional Markdown M188 gate evidence report path")
    operating_defensive_gate_evidence.add_argument("--json-output-path", type=Path, default=Path("exports/m188_operating_defensive_gate_evidence.json"), help="Optional JSON M188 gate evidence report path")
    operating_defensive_gate_evidence.add_argument("--json", action="store_true", help="Print machine-readable M188 gate evidence JSON")

    operating_defensive_gate_retry = sub.add_parser(
        "operating-defensive-gate-retry",
        help="Run M189 defensive gate what-if/option quote retry diagnostics and rebuild strict defensive evidence/ledger",
    )
    operating_defensive_gate_retry.add_argument("--defensive-action-plan-json", type=Path, default=Path("exports/m186_operating_defensive_action_plan.json"))
    operating_defensive_gate_retry.add_argument("--account", help="Optional IBKR account to include in generated what-if order previews")
    operating_defensive_gate_retry.add_argument("--max-preview-items", type=int, default=10)
    operating_defensive_gate_retry.add_argument("--existing-whatif-json", type=Path, help="Optional existing IBKR what-if JSON to include as attempt 0")
    operating_defensive_gate_retry.add_argument("--existing-option-quote-json", type=Path, help="Optional existing option quote snapshot JSON to include as attempt 0")
    operating_defensive_gate_retry.add_argument("--run-ibkr-whatif", action="store_true", help="Explicitly run IBKR what-if attempts with whatIf=True; IBKR requires API transmit=True for what-if calculation but no live order is routed")
    operating_defensive_gate_retry.add_argument("--whatif-attempts", type=int, default=2)
    operating_defensive_gate_retry.add_argument("--include-human-review", action="store_true")
    operating_defensive_gate_retry.add_argument("--allow-fractional", action="store_true")
    operating_defensive_gate_retry.add_argument("--whatif-timeout-seconds", type=float, default=20.0)
    operating_defensive_gate_retry.add_argument("--run-option-quotes", action="store_true", help="Explicitly run read-only option quote snapshot attempts")
    operating_defensive_gate_retry.add_argument("--regular-session-only-option-quotes", action="store_true", help="Defer option bid/ask quote retries outside the US regular option session")
    operating_defensive_gate_retry.add_argument("--option-session-gate-now", help="Optional ISO timestamp for deterministic option quote session-gate evaluation")
    operating_defensive_gate_retry.add_argument("--option-symbols", help="Comma-separated option symbols; defaults to M186 defensive option symbols")
    operating_defensive_gate_retry.add_argument("--option-attempts", type=int, default=2)
    operating_defensive_gate_retry.add_argument("--option-market-data-types", default="1,3,4")
    operating_defensive_gate_retry.add_argument("--option-quote-seconds", type=float, default=10.0)
    operating_defensive_gate_retry.add_argument("--option-chain-timeout-seconds", type=float, default=15.0)
    operating_defensive_gate_retry.add_argument("--min-dte", type=int, default=7)
    operating_defensive_gate_retry.add_argument("--max-dte", type=int, default=60)
    operating_defensive_gate_retry.add_argument("--strikes-around", type=int, default=1)
    operating_defensive_gate_retry.add_argument("--max-expirations", type=int, default=1)
    operating_defensive_gate_retry.add_argument("--max-quote-contracts", type=int, default=30)
    operating_defensive_gate_retry.add_argument("--max-spread-pct", type=float, default=0.25)
    operating_defensive_gate_retry.add_argument("--sleep-seconds", type=float, default=1.0)
    operating_defensive_gate_retry.add_argument("--output-dir", type=Path, default=Path("exports/m189_defensive_gate_retry"))
    operating_defensive_gate_retry.add_argument("--output", type=Path, default=Path("exports/m189_operating_defensive_gate_retry.md"))
    operating_defensive_gate_retry.add_argument("--json-output-path", type=Path, default=Path("exports/m189_operating_defensive_gate_retry.json"))
    operating_defensive_gate_retry.add_argument("--json", action="store_true", help="Print machine-readable M189 retry report JSON")

    operating_option_quote_resume_plan = sub.add_parser(
        "operating-option-quote-resume-plan",
        help="Build an off-hours M193 resume plan for the next regular-session defensive option quote retry",
    )
    operating_option_quote_resume_plan.add_argument("--latest-retry-json", type=Path, default=Path("exports/m191_session_aware_option_quote_gate.json"))
    operating_option_quote_resume_plan.add_argument("--defensive-action-plan-json", type=Path, default=Path("exports/m186_operating_defensive_action_plan.json"))
    operating_option_quote_resume_plan.add_argument("--existing-whatif-json", type=Path, default=Path("exports/m190_defensive_broker_gate_v4/m189_ibkr_defensive_order_whatif_attempt_01.json"))
    operating_option_quote_resume_plan.add_argument("--existing-option-quote-json", type=Path, default=Path("exports/m188_ibkr_defensive_option_quote_snapshot.json"))
    operating_option_quote_resume_plan.add_argument("--account", help="Optional IBKR account to include in generated M192 resume command")
    operating_option_quote_resume_plan.add_argument("--m192-output-dir", type=Path, default=Path("exports/m192_regular_session_option_quote_retry"))
    operating_option_quote_resume_plan.add_argument("--m192-output", type=Path, default=Path("exports/m192_regular_session_option_quote_retry.md"))
    operating_option_quote_resume_plan.add_argument("--m192-json-output-path", type=Path, default=Path("exports/m192_regular_session_option_quote_retry.json"))
    operating_option_quote_resume_plan.add_argument("--option-attempts", type=int, default=3)
    operating_option_quote_resume_plan.add_argument("--option-market-data-types", default="1,3")
    operating_option_quote_resume_plan.add_argument("--option-quote-seconds", type=float, default=20.0)
    operating_option_quote_resume_plan.add_argument("--option-chain-timeout-seconds", type=float, default=30.0)
    operating_option_quote_resume_plan.add_argument("--max-quote-contracts", type=int, default=60)
    operating_option_quote_resume_plan.add_argument("--now", help="Optional ISO timestamp for deterministic next-session calculation")
    operating_option_quote_resume_plan.add_argument("--output", type=Path, default=Path("exports/m193_option_quote_resume_plan.md"))
    operating_option_quote_resume_plan.add_argument("--json-output-path", type=Path, default=Path("exports/m193_option_quote_resume_plan.json"))
    operating_option_quote_resume_plan.add_argument("--json", action="store_true", help="Print machine-readable M193 resume plan JSON")

    execution_safety_cache_supervisor = sub.add_parser(
        "execution-safety-cache-supervisor",
        help="Run the M211 slow pre-order safety-cache supervisor for what-if, short availability, and option quote evidence",
    )
    execution_safety_cache_supervisor.add_argument("--profile-json", type=Path, default=Path("config/execution_safety_cache_supervisor_profile_v1.json"))
    execution_safety_cache_supervisor.add_argument("--max-ticks", type=int, default=1)
    execution_safety_cache_supervisor.add_argument("--interval-seconds", type=float, default=1.0)
    execution_safety_cache_supervisor.add_argument("--sleep-between-ticks", action="store_true")
    execution_safety_cache_supervisor.add_argument("--execute-refresh", action="store_true", help="Execute allowlisted cache refresh commands that are explicitly enabled in the profile")
    execution_safety_cache_supervisor.add_argument("--timeout-seconds", type=float, default=180.0)
    execution_safety_cache_supervisor.add_argument("--tick-log-jsonl-path", type=Path, default=Path("exports/m211_execution_safety_cache_supervisor_ticks.jsonl"))
    execution_safety_cache_supervisor.add_argument("--output", type=Path, default=Path("exports/m211_execution_safety_cache_supervisor.md"))
    execution_safety_cache_supervisor.add_argument("--json-output-path", type=Path, default=Path("exports/m211_execution_safety_cache_supervisor.json"))
    execution_safety_cache_supervisor.add_argument("--json", action="store_true", help="Print machine-readable M211 safety-cache supervisor JSON")
