from __future__ import annotations

from pathlib import Path

from .cli_options_commands import add_options_market_structure_commands


def add_market_data_collection_commands(sub):
    market_data_desk = sub.add_parser(
        "market-data-desk",
        help="Run the read-only Market Data Specialist Desk over global macro/risk instruments",
    )
    market_data_desk.add_argument("--seconds", type=int, default=30, help="Run duration; use 0 for continuous")
    market_data_desk.add_argument("--refresh-seconds", type=float, default=5.0)
    market_data_desk.add_argument("--market-data-type", type=int, default=1, choices=(1, 2, 3, 4))
    market_data_desk.add_argument("--client-id", type=int, help="Override IBKR clientId")
    market_data_desk.add_argument("--extra-stock-symbols", help="Comma-separated additional SMART/USD stock or ETF symbols to monitor, e.g. account option underlyings")
    market_data_desk.add_argument("--account-snapshot-json", type=Path, help="Read account option positions and automatically add their underlying symbols as SMART/USD stock marks")
    market_data_desk.add_argument("--gap-threshold-pct", type=float, default=1.0)
    market_data_desk.add_argument("--tick-move-threshold-pct", type=float, default=0.5)
    market_data_desk.add_argument("--fallback-after-seconds", type=float, default=8.0, help="Retry the next IBKR contract candidate when a subscription has no price after this many seconds")
    market_data_desk.add_argument(
        "--fallback-on-close-only",
        action="store_true",
        help="Also retry the next contract candidate when only a close price is available; off by default to avoid holiday/off-hours churn",
    )
    market_data_desk.add_argument("--output", type=Path, help="Optional Markdown path for the latest desk report")
    market_data_desk.add_argument("--json-output-path", type=Path, help="Optional JSON path for the latest desk snapshot")
    market_data_desk.add_argument("--json", action="store_true", help="Print machine-readable final desk snapshot JSON")
    add_options_market_structure_commands(sub)
    evidence_freshness_desk = sub.add_parser(
        "evidence-freshness-desk",
        help="Build a read-only freshness report for news, geopolitical, macro, regime, technical, and market-data evidence",
    )
    evidence_freshness_desk.add_argument("--profile-json", type=Path, help="Optional freshness desk profile with default paths")
    evidence_freshness_desk.add_argument("--database-path", type=Path)
    evidence_freshness_desk.add_argument("--market-data-json", type=Path)
    evidence_freshness_desk.add_argument("--economic-desk-json", type=Path)
    evidence_freshness_desk.add_argument("--output", type=Path, help="Optional Markdown evidence freshness report path")
    evidence_freshness_desk.add_argument("--json-output-path", type=Path, help="Optional JSON evidence freshness report path")
    evidence_freshness_desk.add_argument("--json", action="store_true", help="Print machine-readable freshness report JSON")
    evidence_refresh_queue = sub.add_parser(
        "evidence-refresh-queue",
        help="Plan or execute read-only refresh jobs for stale Evidence Freshness Desk sources",
    )
    evidence_refresh_queue.add_argument("--profile-json", type=Path, help="Optional evidence refresh queue profile")
    evidence_refresh_queue.add_argument("--freshness-report-json", type=Path)
    evidence_refresh_queue.add_argument("--evidence-freshness-profile-json", type=Path)
    evidence_refresh_queue.add_argument("--dashboard-profile-json", type=Path)
    evidence_refresh_queue.add_argument("--output-dir", type=Path)
    evidence_refresh_queue.add_argument("--market-data-seconds", type=int)
    evidence_refresh_queue.add_argument("--external-limit", type=int)
    evidence_refresh_queue.add_argument("--external-timespan")
    evidence_refresh_queue.add_argument("--ibkr-news-limit-per-symbol", type=int)
    evidence_refresh_queue.add_argument("--technical-start-days", type=int)
    evidence_refresh_queue.add_argument("--execute", action="store_true", help="Execute planned read-only refresh jobs in priority order")
    evidence_refresh_queue.add_argument("--max-jobs", type=int, help="Optional cap for execution/planning")
    evidence_refresh_queue.add_argument("--job-timeout-seconds", type=float)
    evidence_refresh_queue.add_argument("--skip-live-ibkr", action="store_true", help="Skip jobs that need a live IBKR read-only session")
    evidence_refresh_queue.add_argument("--output", type=Path, help="Optional Markdown queue report path")
    evidence_refresh_queue.add_argument("--json-output-path", type=Path, help="Optional JSON queue report path")
    evidence_refresh_queue.add_argument("--json", action="store_true", help="Print machine-readable queue report JSON")
    evidence_refresh_watchdog = sub.add_parser(
        "evidence-refresh-watchdog",
        help="Run finite Evidence Freshness Watchdog ticks with cooldown/backoff and optional refresh queue execution",
    )
    evidence_refresh_watchdog.add_argument("--profile-json", type=Path, help="Optional evidence refresh watchdog profile")
    evidence_refresh_watchdog.add_argument("--freshness-profile-json", type=Path)
    evidence_refresh_watchdog.add_argument("--refresh-queue-profile-json", type=Path)
    evidence_refresh_watchdog.add_argument("--freshness-report-json", type=Path)
    evidence_refresh_watchdog.add_argument("--refresh-queue-json", type=Path)
    evidence_refresh_watchdog.add_argument("--refresh-queue-markdown", type=Path)
    evidence_refresh_watchdog.add_argument("--state-json-path", type=Path)
    evidence_refresh_watchdog.add_argument("--tick-log-jsonl-path", type=Path)
    evidence_refresh_watchdog.add_argument("--max-ticks", type=int)
    evidence_refresh_watchdog.add_argument("--interval-seconds", type=float)
    evidence_refresh_watchdog.add_argument("--sleep-between-ticks", action="store_true", default=None)
    evidence_refresh_watchdog.add_argument("--cooldown-seconds", type=float)
    evidence_refresh_watchdog.add_argument("--initial-backoff-seconds", type=float)
    evidence_refresh_watchdog.add_argument("--max-backoff-seconds", type=float)
    evidence_refresh_watchdog.add_argument("--queue-timeout-seconds", type=float)
    evidence_refresh_watchdog.add_argument("--dry-run", action="store_true", help="Plan queue without executing refresh jobs")
    evidence_refresh_watchdog.add_argument("--skip-freshness-recheck", action="store_true")
    evidence_refresh_watchdog.add_argument("--run-id")
    evidence_refresh_watchdog.add_argument("--output", type=Path, help="Optional Markdown watchdog report path")
    evidence_refresh_watchdog.add_argument("--json-output-path", type=Path, help="Optional JSON watchdog report path")
    evidence_refresh_watchdog.add_argument("--json", action="store_true", help="Print machine-readable watchdog report JSON")
    source_ingest_health = sub.add_parser(
        "source-ingest-health",
        help="Diagnose source-by-source ingest health and online-required evidence gaps",
    )
    source_ingest_health.add_argument("--profile-json", type=Path, help="Optional M160 source ingest health profile")
    source_ingest_health.add_argument("--freshness-report-json", type=Path)
    source_ingest_health.add_argument("--refresh-queue-json", type=Path)
    source_ingest_health.add_argument("--watchdog-json", type=Path)
    source_ingest_health.add_argument("--live-validation-json", type=Path)
    source_ingest_health.add_argument("--online-required-sources", help="Comma-separated realtime-required source keys")
    source_ingest_health.add_argument("--batch-sources", help="Comma-separated batch/slow source keys")
    source_ingest_health.add_argument("--refresh-freshness-before-check", action="store_true")
    source_ingest_health.add_argument("--max-ticks", type=int)
    source_ingest_health.add_argument("--interval-seconds", type=float)
    source_ingest_health.add_argument("--sleep-between-ticks", action="store_true", default=None)
    source_ingest_health.add_argument("--run-id")
    source_ingest_health.add_argument("--output", type=Path, help="Optional Markdown source ingest health report path")
    source_ingest_health.add_argument("--json-output-path", type=Path, help="Optional JSON source ingest health report path")
    source_ingest_health.add_argument("--tick-log-jsonl-path", type=Path)
    source_ingest_health.add_argument("--json", action="store_true", help="Print machine-readable report JSON")
    source_live_validation = sub.add_parser(
        "source-live-validation",
        help="Read-only live provider validation for IBKR/Benzinga/Dow Jones and Marketaux; Alpha Vantage is disabled by default",
    )
    source_live_validation.add_argument("--profile-json", type=Path, help="Optional M161 source live validation profile")
    source_live_validation.add_argument("--symbols", help="Comma-separated IBKR news symbols")
    source_live_validation.add_argument("--providers", help="Comma-separated IBKR news provider codes")
    source_live_validation.add_argument("--external-symbols", help="Comma-separated Marketaux symbols")
    source_live_validation.add_argument("--alpha-tickers", help="Legacy/explicit-only Alpha Vantage tickers; active validation skips Alpha by default")
    source_live_validation.add_argument("--limit", type=int)
    source_live_validation.add_argument("--ibkr-timeout-seconds", type=float)
    source_live_validation.add_argument("--external-timeout-seconds", type=float)
    source_live_validation.add_argument("--ibkr-client-id", type=int)
    source_live_validation.add_argument("--skip-ibkr", action="store_true")
    source_live_validation.add_argument("--skip-marketaux", action="store_true")
    source_live_validation.add_argument("--skip-alpha-vantage", action="store_true")
    source_live_validation.add_argument("--output", type=Path, help="Optional Markdown provider validation report path")
    source_live_validation.add_argument("--json-output-path", type=Path, help="Optional JSON provider validation report path")
    source_live_validation.add_argument("--json", action="store_true", help="Print machine-readable report JSON")
    source_online_supervisor = sub.add_parser(
        "source-online-supervisor",
        help="Keep online-required evidence sources live by validating providers, running catch-up actions, and rechecking source health",
    )
    source_online_supervisor.add_argument("--profile-json", type=Path, help="Optional M162 source online supervisor profile")
    source_online_supervisor.add_argument("--max-ticks", type=int)
    source_online_supervisor.add_argument("--interval-seconds", type=float)
    source_online_supervisor.add_argument("--sleep-between-ticks", action="store_true", default=None)
    source_online_supervisor.add_argument("--execute-actions", action="store_true", help="Execute evidence refresh/catch-up actions instead of dry-run planning")
    source_online_supervisor.add_argument("--skip-live-ibkr", action="store_true")
    source_online_supervisor.add_argument("--job-timeout-seconds", type=float)
    source_online_supervisor.add_argument("--run-id")
    source_online_supervisor.add_argument("--output", type=Path, help="Optional Markdown supervisor report path")
    source_online_supervisor.add_argument("--json-output-path", type=Path, help="Optional JSON supervisor report path")
    source_online_supervisor.add_argument("--json", action="store_true", help="Print machine-readable supervisor JSON")


def add_market_evidence_gate_commands(sub):
    evidence_preflight = sub.add_parser(
        "evidence-preflight",
        help="Build a read-only operating preflight gate from evidence freshness and auto-recovery state",
    )
    evidence_preflight.add_argument("--profile-json", type=Path, help="Optional evidence preflight profile")
    evidence_preflight.add_argument("--freshness-report-json", type=Path)
    evidence_preflight.add_argument("--watchdog-state-json", type=Path)
    evidence_preflight.add_argument("--allow-stale-evidence", action="store_true", help="Do not block on stale sources")
    evidence_preflight.add_argument("--allow-offline-evidence", action="store_true", help="Do not block on offline sources")
    evidence_preflight.add_argument("--allow-watchdog-failure", action="store_true", help="Do not block on watchdog failure")
    evidence_preflight.add_argument("--no-lagging-haircut", action="store_true", help="Allow lagging sources without size haircut")
    evidence_preflight.add_argument("--lagging-size-multiplier", type=float)
    evidence_preflight.add_argument("--min-source-weight", type=float)
    evidence_preflight.add_argument("--output", type=Path, help="Optional Markdown evidence preflight report path")
    evidence_preflight.add_argument("--json-output-path", type=Path, help="Optional JSON evidence preflight report path")
    evidence_preflight.add_argument("--json", action="store_true", help="Print machine-readable evidence preflight JSON")
    market_data_risk_gate = sub.add_parser(
        "market-data-risk-gate",
        help="Build a read-only action gate from Market Data Desk quality and temperature state",
    )
    market_data_risk_gate.add_argument("--profile-json", type=Path, help="Optional market data risk gate profile")
    market_data_risk_gate.add_argument("--market-data-json", type=Path)
    market_data_risk_gate.add_argument("--market-data-session-overlay-json", type=Path)
    market_data_risk_gate.add_argument("--allow-delayed-market-data", action="store_true", default=None, help="Do not block on delayed market data")
    market_data_risk_gate.add_argument("--allow-missing-market-prices", action="store_true", default=None, help="Do not block on missing market prices")
    market_data_risk_gate.add_argument("--allow-warming-up-market-data", action="store_true", default=None, help="Do not block while subscriptions are warming up")
    market_data_risk_gate.add_argument("--allow-market-data-errors", action="store_true", default=None, help="Do not block on IBKR contract/subscription errors")
    market_data_risk_gate.add_argument("--allow-stale-market-data", action="store_true", default=None, help="Do not block on stale market data")
    market_data_risk_gate.add_argument("--allow-close-only-quotes", action="store_true", default=None, help="Do not block on close-only fallback quotes")
    market_data_risk_gate.add_argument("--allow-severe-wide-spread", action="store_true", default=None, help="Do not block on severe wide-spread quotes")
    market_data_risk_gate.add_argument("--watch-size-multiplier", type=float)
    market_data_risk_gate.add_argument("--hot-size-multiplier", type=float)
    market_data_risk_gate.add_argument("--defensive-size-multiplier", type=float)
    market_data_risk_gate.add_argument("--stress-score-threshold", type=float)
    market_data_risk_gate.add_argument("--output", type=Path, help="Optional Markdown market data risk gate report path")
    market_data_risk_gate.add_argument("--json-output-path", type=Path, help="Optional JSON market data risk gate report path")
    market_data_risk_gate.add_argument("--json", action="store_true", help="Print machine-readable market data risk gate JSON")
    market_data_session_overlay = sub.add_parser(
        "market-data-session-overlay",
        help="Interpret Market Data Desk stress by session and asset class without disabling stock/ETF overnight monitoring",
    )
    market_data_session_overlay.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    market_data_session_overlay.add_argument("--now", help="Optional ISO timestamp for deterministic US session evaluation")
    market_data_session_overlay.add_argument("--output", type=Path, default=Path("exports/m194_market_data_session_overlay.md"))
    market_data_session_overlay.add_argument("--json-output-path", type=Path, default=Path("exports/m194_market_data_session_overlay.json"))
    market_data_session_overlay.add_argument("--json", action="store_true", help="Print machine-readable M194 session overlay JSON")
    market_data_session_transition_gate = sub.add_parser(
        "market-data-session-transition-gate",
        help="Build a read-only session-transition gate that requires fresh Market Data/Risk/Operating gates before auto-release",
    )
    market_data_session_transition_gate.add_argument("--market-data-session-overlay-json", type=Path, default=Path("exports/m194_market_data_session_overlay.json"))
    market_data_session_transition_gate.add_argument("--state-json-path", type=Path, default=Path("exports/m198_market_data_session_transition_state.json"))
    market_data_session_transition_gate.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m175_dashboard_market_data_risk_gate_latest.json"))
    market_data_session_transition_gate.add_argument("--market-data-operating-auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"))
    market_data_session_transition_gate.add_argument("--now", help="Optional ISO timestamp for deterministic transition evaluation")
    market_data_session_transition_gate.add_argument("--max-session-overlay-age-seconds", type=float, default=180.0)
    market_data_session_transition_gate.add_argument("--max-risk-gate-age-seconds", type=float, default=180.0)
    market_data_session_transition_gate.add_argument("--max-operating-gate-age-seconds", type=float, default=180.0)
    market_data_session_transition_gate.add_argument("--no-state-update", action="store_true", help="Do not update the transition state file")
    market_data_session_transition_gate.add_argument("--output", type=Path, default=Path("exports/m198_market_data_session_transition_gate.md"))
    market_data_session_transition_gate.add_argument("--json-output-path", type=Path, default=Path("exports/m198_market_data_session_transition_gate.json"))
    market_data_session_transition_gate.add_argument("--json", action="store_true", help="Print machine-readable M198 transition gate JSON")
    market_data_session_transition_closed_loop = sub.add_parser(
        "market-data-session-transition-closed-loop",
        help="Run the safe read-only/paper rebuild chain when the M198 session-transition gate requires rebuild",
    )
    market_data_session_transition_closed_loop.add_argument("--market-data-json", type=Path, default=Path("exports/m153_market_data_desk_refresh.json"))
    market_data_session_transition_closed_loop.add_argument("--market-data-session-overlay-json", type=Path, default=Path("exports/m194_market_data_session_overlay.json"))
    market_data_session_transition_closed_loop.add_argument("--state-json-path", type=Path, default=Path("exports/m199_market_data_session_transition_state.json"))
    market_data_session_transition_closed_loop.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m197_market_data_risk_gate_session_aware.json"))
    market_data_session_transition_closed_loop.add_argument("--market-data-operating-auto-gate-json", type=Path, default=Path("exports/m197_market_data_operating_auto_gate_session_aware.json"))
    market_data_session_transition_closed_loop.add_argument("--preview-json", type=Path, default=Path("exports/m112_m111_order_preview.json"))
    market_data_session_transition_closed_loop.add_argument("--short-availability-json", type=Path, default=Path("exports/m113_ibkr_short_availability.json"))
    market_data_session_transition_closed_loop.add_argument("--evidence-preflight-json", type=Path, default=Path("exports/m156_evidence_preflight_latest.json"))
    market_data_session_transition_closed_loop.add_argument("--output-dir", type=Path, default=Path("exports"))
    market_data_session_transition_closed_loop.add_argument("--dry-run", action="store_true", help="Report the need for rebuild without writing rebuilt gate artifacts")
    market_data_session_transition_closed_loop.add_argument("--max-session-overlay-age-seconds", type=float, default=180.0)
    market_data_session_transition_closed_loop.add_argument("--max-risk-gate-age-seconds", type=float, default=180.0)
    market_data_session_transition_closed_loop.add_argument("--max-operating-gate-age-seconds", type=float, default=180.0)
    market_data_session_transition_closed_loop.add_argument("--max-auto-orders", type=int, default=10)
    market_data_session_transition_closed_loop.add_argument("--min-cash-buffer", type=float, default=50.0)
    market_data_session_transition_closed_loop.add_argument("--allowed-ticket-types", default="stock")
    market_data_session_transition_closed_loop.add_argument("--allowed-actions", default="BUY,SELL")
    market_data_session_transition_closed_loop.add_argument("--sample-limit", type=int, default=20)
    market_data_session_transition_closed_loop.add_argument("--output", type=Path, default=Path("exports/m199_market_data_session_transition_closed_loop.md"))
    market_data_session_transition_closed_loop.add_argument("--json-output-path", type=Path, default=Path("exports/m199_market_data_session_transition_closed_loop.json"))
    market_data_session_transition_closed_loop.add_argument("--json", action="store_true", help="Print machine-readable M199 closed-loop JSON")
    session_transition_release_governor = sub.add_parser(
        "session-transition-release-governor",
        help="Build the final read-only release governor before ordinary paper alpha is reopened after a session transition",
    )
    session_transition_release_governor.add_argument("--transition-gate-json", type=Path, default=Path("exports/m199_market_data_session_transition_gate_final.json"))
    session_transition_release_governor.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m175_dashboard_market_data_risk_gate_latest.json"))
    session_transition_release_governor.add_argument("--market-data-operating-auto-gate-json", type=Path, default=Path("exports/m182_market_data_operating_auto_gate_latest.json"))
    session_transition_release_governor.add_argument("--portfolio-risk-state-json", type=Path, default=Path("exports/m123_paper_portfolio_risk_state.json"))
    session_transition_release_governor.add_argument("--evidence-preflight-json", type=Path, default=Path("exports/m156_evidence_preflight_latest.json"))
    session_transition_release_governor.add_argument("--operating-final-preflight-json", type=Path)
    session_transition_release_governor.add_argument("--now", help="Optional ISO timestamp for deterministic release-governor evaluation")
    session_transition_release_governor.add_argument("--output", type=Path, default=Path("exports/m201_session_transition_release_governor.md"))
    session_transition_release_governor.add_argument("--json-output-path", type=Path, default=Path("exports/m201_session_transition_release_governor.json"))
    session_transition_release_governor.add_argument("--json", action="store_true", help="Print machine-readable M201 release governor JSON")
    release_governed_recovery_closed_loop = sub.add_parser(
        "release-governed-recovery-closed-loop",
        help="Run the M206 read-only closed loop that refreshes the release governor, rebuilds the release-governed paper ledger/router, and post-validates stale pending clearance",
    )
    release_governed_recovery_closed_loop.add_argument("--transition-gate-json", type=Path, default=Path("exports/m199_market_data_session_transition_gate_final.json"))
    release_governed_recovery_closed_loop.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m199_market_data_risk_gate.json"))
    release_governed_recovery_closed_loop.add_argument("--market-data-operating-auto-gate-json", type=Path, default=Path("exports/m199_market_data_operating_auto_gate.json"))
    release_governed_recovery_closed_loop.add_argument("--portfolio-risk-state-json", type=Path, default=Path("exports/m123_paper_portfolio_risk_state.json"))
    release_governed_recovery_closed_loop.add_argument("--evidence-preflight-json", type=Path, default=Path("exports/m156_evidence_preflight_latest.json"))
    release_governed_recovery_closed_loop.add_argument("--operating-final-preflight-json", type=Path)
    release_governed_recovery_closed_loop.add_argument("--snapshot-json", type=Path, default=Path("exports/m205_trading_dashboard_snapshot_after_profile.json"))
    release_governed_recovery_closed_loop.add_argument("--existing-ledger-json", type=Path, default=Path("exports/m204_session_transition_release_governed_execution_intent_ledger.json"))
    release_governed_recovery_closed_loop.add_argument("--existing-router-json", type=Path, default=Path("exports/m204_session_transition_release_governed_paper_order_router.json"))
    release_governed_recovery_closed_loop.add_argument("--marks-json", type=Path)
    release_governed_recovery_closed_loop.add_argument("--release-governor-output", type=Path, default=Path("exports/m206_session_transition_release_governor.md"))
    release_governed_recovery_closed_loop.add_argument("--release-governor-json-output-path", type=Path, default=Path("exports/m206_session_transition_release_governor.json"))
    release_governed_recovery_closed_loop.add_argument("--ledger-output", type=Path, default=Path("exports/m206_release_governed_execution_intent_ledger.md"))
    release_governed_recovery_closed_loop.add_argument("--ledger-json-output-path", type=Path, default=Path("exports/m206_release_governed_execution_intent_ledger.json"))
    release_governed_recovery_closed_loop.add_argument("--router-output", type=Path, default=Path("exports/m206_release_governed_paper_order_router.md"))
    release_governed_recovery_closed_loop.add_argument("--router-json-output-path", type=Path, default=Path("exports/m206_release_governed_paper_order_router.json"))
    release_governed_recovery_closed_loop.add_argument("--output", type=Path, default=Path("exports/m206_release_governed_recovery_closed_loop.md"))
    release_governed_recovery_closed_loop.add_argument("--json-output-path", type=Path, default=Path("exports/m206_release_governed_recovery_closed_loop.json"))
    release_governed_recovery_closed_loop.add_argument("--monitor-size-multiplier", type=float, default=0.50)
    release_governed_recovery_closed_loop.add_argument("--max-intents", type=int, default=10)
    release_governed_recovery_closed_loop.add_argument("--max-orders", type=int, default=10)
    release_governed_recovery_closed_loop.add_argument("--sample-limit", type=int, default=20)
    release_governed_recovery_closed_loop.add_argument("--ignore-operating-preflight", action="store_true", help="Disable M184 operating final preflight for diagnostic runs")
    release_governed_recovery_closed_loop.add_argument("--allow-non-operating-source-ledger", action="store_true")
    release_governed_recovery_closed_loop.add_argument("--ignore-portfolio-risk-gate", action="store_true", help="Disable required M123 portfolio-risk gate for diagnostic runs")
    release_governed_recovery_closed_loop.add_argument("--ignore-session-transition-release-governor", action="store_true", help="Disable required M201/M204 release-governor preflight for diagnostic runs")
    release_governed_recovery_closed_loop.add_argument(
        "--fill-price-source",
        choices=("local_estimate", "marks_or_local_estimate", "require_marks"),
        default="local_estimate",
    )
    release_governed_recovery_closed_loop.add_argument("--now", help="Optional ISO timestamp for deterministic M206 evaluation")
    release_governed_recovery_closed_loop.add_argument("--json", action="store_true", help="Print machine-readable M206 closed-loop JSON")
    market_data_defensive_mode = sub.add_parser(
        "market-data-defensive-mode",
        help="Build the M181 market-data defensive operating mode from the Market Data Risk Gate",
    )
    market_data_defensive_mode.add_argument("--market-data-risk-gate-json", type=Path, default=Path("exports/m175_dashboard_market_data_risk_gate_latest.json"))
    market_data_defensive_mode.add_argument("--state-json-path", type=Path, default=Path("exports/m181_market_data_defensive_mode_state.json"))
    market_data_defensive_mode.add_argument("--persistent-warning-seconds", type=float, default=300.0)
    market_data_defensive_mode.add_argument("--persistent-critical-seconds", type=float, default=900.0)
    market_data_defensive_mode.add_argument("--output", type=Path, default=Path("exports/m181_market_data_defensive_mode_latest.md"), help="Optional Markdown defensive mode report path")
    market_data_defensive_mode.add_argument("--json-output-path", type=Path, default=Path("exports/m181_market_data_defensive_mode_latest.json"), help="Optional JSON defensive mode report path")
    market_data_defensive_mode.add_argument("--json", action="store_true", help="Print machine-readable defensive mode JSON")
