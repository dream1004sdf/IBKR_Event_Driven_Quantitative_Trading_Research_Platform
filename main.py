from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from recorder.cli_broker_safety_commands import add_ibkr_broker_evidence_commands, add_operating_defensive_safety_commands
from recorder.cli_capital_commands import (
    add_capital_commands,
    command_capital_margin_state,
    command_capital_pipeline_orchestrator,
    command_capital_trade_executable_separation,
    command_live_read_only_capital_validation,
)
from recorder.cli_command_registry import dispatch_command
from recorder.cli_ai_runtime import (
    command_ai_diagnose,
    command_ai_once,
    command_ai_report,
    command_ai_retry_dead,
    command_ai_run,
    command_ai_self_test,
)
from recorder.cli_core_commands import add_core_data_commands
from recorder.cli_data_runtime import (
    command_attribution_backfill,
    command_attribution_report,
    command_cluster_report,
    command_official_add,
    command_official_report,
    command_sec_sync,
    command_sec_report,
    command_macro_sync,
    command_macro_report,
    command_macro_lineage_v1_rebuild,
    command_financial_facts_sync,
    command_financial_facts_report,
    command_fred_sync,
    command_fred_report,
    command_fred_universe_sync,
    command_fred_universe_report,
    command_economic_indicator_desk,
    command_international_macro_sync,
    command_international_macro_report,
    command_technical_indicators_build,
    command_technical_indicators_report,
    command_external_market_sync,
    command_external_market_backfill,
    command_external_market_coverage,
    command_standing_regime_build,
    command_standing_regime_report,
    command_standing_regime_discover_blind,
    command_standing_regime_outcomes,
    command_geopolitical_intelligence,
    command_benzinga_external_signals_build,
    command_dow_jones_external_signals_build,
    command_breaking_news_ingest,
    command_emergency_breaking_backfill,
    command_external_market_report,
    command_historical_import,
    command_historical_report,
    command_replay_corpus_build,
    command_replay_corpus_report,
    command_replay_outcome_build,
    command_replay_outcome_report,
    command_replay_outcome_compare,
    command_replay_dataset_export,
)
from recorder.cli_dashboard_commands import add_dashboard_commands
from recorder.cli_dashboard_runtime import (
    command_dashboard_action_queue,
    command_dashboard_latency_safety_audit,
    command_dashboard_recovery_closed_loop,
    command_dashboard_recovery_executor,
    command_dashboard_recovery_plan,
    command_dashboard_recovery_post_validation,
    command_dashboard_supervisor_handoff_audit,
    command_execution_hot_path_latency_audit,
    command_trading_dashboard_html,
    command_trading_dashboard_snapshot,
)
from recorder.cli_dashboard_hook_helpers import (
    _run_dashboard_release_governed_recovery_closed_loop,
    _run_dashboard_session_transition_closed_loop,
    _run_dashboard_session_transition_release_governor,
)
from recorder.cli_dashboard_loop_runtime import command_trading_dashboard_loop
from recorder.cli_training_commands import add_training_commands
from recorder.cli_market_evidence_commands import add_market_data_collection_commands, add_market_evidence_gate_commands
from recorder.cli_live_learning_runtime import (
    command_live_scorer_learning,
    command_live_learning_llm_notes,
    command_threshold_candidate_evaluator,
    command_replay_standing_regime_family_gate_tune,
    command_walk_forward_learning_sim,
    command_walk_forward_adjustment_notes,
    command_walk_forward_cycle_train,
    command_walk_forward_cycle_threshold_tune,
    command_walk_forward_cycle_nuanced_train,
)
from recorder.cli_market_evidence_runtime import (
    command_market_data_operating_auto_gate,
    command_evidence_freshness_desk,
    command_evidence_preflight,
    command_evidence_refresh_queue,
    command_evidence_refresh_watchdog,
    command_market_data_desk,
    command_market_data_defensive_mode,
    command_market_data_risk_gate,
    command_market_data_session_overlay,
    command_market_data_session_transition_closed_loop,
    command_market_data_session_transition_gate,
    command_release_governed_recovery_closed_loop,
    command_session_transition_release_governor,
    command_source_ingest_health,
    command_source_live_validation,
    command_source_online_supervisor,
)
from recorder.cli_news_runtime import (
    command_ibkr_news_backfill,
    command_ibkr_news_broadtape_probe,
    command_news_subscription_mesh,
    command_cross_provider_event_roles,
    command_news_lineage_v2_rebuild,
    command_reaction_label_foundation_build,
)
from recorder.cli_news_market_outcome_runtime import (
    command_clean_backfill_cycle_audit,
    command_clean_backfill_cycle_report,
    command_clean_signal_classification_audit,
    command_news_market_outcome_backfill,
    command_news_symbol_tier_report,
)
from recorder.cli_news_commands import add_external_market_commands, add_news_source_commands
from recorder.cli_options_commands import add_ibkr_option_snapshot_commands, add_options_market_structure_commands
from recorder.cli_options_runtime import (
    command_ibkr_options_chain_scan,
    command_ibkr_options_quote_snapshot,
    command_ibkr_account_snapshot,
    command_ibkr_order_whatif_preview,
    command_ibkr_short_availability,
    command_ibkr_stock_mark_snapshot,
    command_options_market_structure_desk,
    command_options_market_structure_supervisor,
    command_option_structure_outcome_build,
    command_option_structure_outcome_report,
    command_single_stock_option_watchlist,
    command_single_stock_option_watchlist_loop,
)
from recorder.cli_operating_defensive_runtime import (
    command_execution_safety_cache_supervisor,
    command_operating_defensive_action_plan,
    command_operating_defensive_intent_ledger,
    command_operating_defensive_gate_evidence,
    command_operating_defensive_gate_retry,
    command_operating_option_quote_resume_plan,
)
from recorder.cli_operational_admin_commands import (
    add_operational_admin_commands,
    command_ai_cost_report,
    command_artifact_cleanup,
    command_code_health_report,
    command_future_earnings_calendar,
    command_prediction_trend_report,
    command_singleton_lock_cleanup,
    command_singleton_lock_status,
)
from recorder.cli_paper_cycle_runtime import (
    _apply_mark_refresh_quality_action_to_portfolio_risk,
    _build_watch_evidence_preflight_auto_gate,
    _build_watch_market_data_risk_auto_gate,
    _evaluate_mark_refresh_policy,
    _evaluate_paper_session_gate,
    _mark_refresh_apply_partial_fallback,
    _mark_refresh_quality_execution_action,
    _parse_path_list,
    _run_watch_evidence_refresh_watchdog,
    _write_component_json,
    command_replay_score_paper_cycle_runner,
    command_replay_score_paper_cycle_scheduler,
    command_replay_score_paper_cycle_watch_daemon,
    command_replay_score_paper_cycle_watch_runner,
    command_replay_score_paper_cycle_watch_tick,
    command_replay_score_paper_multi_cycle_runner,
)
from recorder.cli_replay_execution_commands import add_replay_execution_commands
from recorder.cli_replay_score_runtime import (
    _parse_float_grid,
    _parse_float_tuple,
    command_replay_score_build,
    command_replay_score_postscore_transform,
    command_replay_score_postscore_threshold_tune,
    command_replay_score_report,
    command_replay_score_leaderboard,
    command_replay_score_calibration,
    command_replay_score_oos,
    command_replay_score_pair_compare,
    command_replay_score_variant_compare,
    command_replay_score_error_analysis,
    command_replay_score_action_gate,
    command_replay_score_paper_action_sim,
    command_replay_score_paper_orders_build,
    command_replay_score_paper_order_tune,
    command_replay_score_paper_order_walk_forward_tune,
    command_replay_score_paper_risk_layer,
    command_replay_score_paper_portfolio_risk_layer,
    command_replay_score_options_analyze,
    command_replay_score_strategy_allocate,
    command_replay_score_strategy_walk_forward_tune,
    command_replay_score_strategy_liquidity_gate,
    command_replay_score_strategy_leg_quote_gate,
    command_replay_score_strategy_leg_aware_walk_forward_tune,
    command_replay_score_execution_readiness,
    command_replay_score_mock_portfolio_readiness,
    command_replay_score_account_backed_portfolio_readiness,
    command_replay_score_account_scaled_position_sizing,
    command_replay_score_order_preview,
    command_replay_score_auto_execution_gate,
    command_replay_score_capital_aware_action_gate,
    command_replay_score_execution_intent_ledger,
    command_replay_score_paper_capital_aware_execution,
    command_replay_score_paper_order_router,
    command_replay_score_paper_position_mark_timeseries,
    command_replay_score_paper_position_mark,
    command_replay_score_paper_exit_intent_ledger,
    command_replay_score_paper_exit_router,
    command_replay_score_paper_portfolio_state,
    command_replay_score_paper_portfolio_risk_state,
    command_replay_score_paper_portfolio_risk_loop,
)
from recorder.cli_strategy_allocation_runtime import (
    command_cross_asset_expression_outcomes,
    command_cross_asset_expression_ranking,
    command_limited_auto_trading_readiness,
    command_micro_option_candidates,
    command_option_whatif_safety_validation,
    command_paper_option_ticket_preview,
    command_strategy_allocation_engine,
    command_ticker_action_candidates,
    command_ticker_candidate_ranking,
)
from recorder.cli_replay_overlay_runtime import (
    command_replay_emergency_overlay,
    command_replay_evidence_packets,
    command_replay_market_overlay,
    command_replay_surprise_adjustments_build,
    command_replay_surprise_adjustments_report,
    command_ytd_calibration_run,
)
from recorder.cli_strategy_allocation_commands import add_strategy_allocation_commands
from recorder.cli_training_runtime import (
    command_m236_news_learning,
    command_m236_outcome_attach,
    command_macro_event_calendar,
    command_macro_release_interpreter,
    command_unknown_theme_discovery,
)
from recorder.config import Settings
from recorder.database import Database
from recorder.self_test import run_self_test


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    # Keep application INFO logs while suppressing thousands of raw ibapi tick messages.
    for name in (
        "ibapi",
        "ibapi.wrapper",
        "ibapi.client",
        "ibapi.connection",
        "ibapi.decoder",
        "httpcore",
        "httpx",
        "openai._base_client",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)


def _console_safe_text(text: str) -> str:
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding, errors="replace")


def command_run(settings: Settings) -> None:
    from recorder.ibkr_app import IBKRMarketEventRecorder
    app = IBKRMarketEventRecorder(settings)
    app.start()
    app.wait_forever()


def command_self_test() -> None:
    result = run_self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))




def command_report(settings: Settings, limit: int) -> None:
    db = Database(settings.database_path)
    print(json.dumps(db.summary(), indent=2, ensure_ascii=False))
    print("\nRecent canonical news:")
    for row in db.recent_news(limit):
        print(
            f"#{row['id']} {row['received_at']} "
            f"[{row['primary_symbol']}/{row['provider']}] {row['headline']}"
        )


def command_diagnose(settings: Settings, seconds: int) -> None:
    from recorder.ibkr_app import IBKRMarketEventRecorder
    print(
        f"Read-only diagnostic: connecting to {settings.host}:{settings.port} "
        f"with clientId={settings.client_id} for {seconds}s"
    )
    app = IBKRMarketEventRecorder(settings)
    app.start()
    try:
        time.sleep(seconds)
    finally:
        app.stop()
    db = Database(settings.database_path)
    print("\nProviders returned and stored:")
    providers = db.list_providers()
    if not providers:
        print("  none (check API news subscriptions and connection logs)")
    for row in providers:
        print(f"  {row['code']}: {row['name']}")
    print("\nDatabase summary:")
    print(json.dumps(db.summary(), indent=2, ensure_ascii=False))




























































































































































































































































































































































































































def command_db_audit(settings: Settings, strict: bool = False) -> None:
    from recorder.db_audit import audit_database

    result = audit_database(settings.database_path)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if strict and (result["severity_counts"]["critical"] or result["severity_counts"]["error"]):
        raise SystemExit(2)


def command_db_repair(settings: Settings) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.db_audit import audit_database

    db = AnalysisDatabase(settings.database_path)
    governed = db.backfill_reaction_governance()
    missed = db.finalize_overdue_targets(
        now=datetime.now(timezone.utc),
        max_lateness_seconds=settings.reaction_max_lateness_seconds,
    )
    rebuilt, failed = db.rebuild_event_clusters()
    if failed:
        raise SystemExit(f"Cluster rebuild failed for {failed} analysis row(s)")
    print(json.dumps({
        "governed_news": governed,
        "overdue_targets_marked_missed": missed,
        "clusters_rebuilt_from_analyses": rebuilt,
        "audit": audit_database(settings.database_path),
    }, indent=2, ensure_ascii=False))


def command_db_backup(settings: Settings, backup_dir: str, keep: int) -> None:
    from recorder.db_backup import backup_database

    result = backup_database(settings.database_path, backup_dir, keep=keep)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_init_env(target: Path) -> None:
    example = Path(__file__).with_name(".env.example")
    if target.exists():
        raise SystemExit(f"Refusing to overwrite existing file: {target}")
    shutil.copyfile(example, target)
    print(f"Created {target}. Review the settings before running.")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Read-only IBKR market event recorder + tiered AI event analyzer"
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("run", help="Connect to IB Gateway and record news/reactions")
    sub.add_parser("self-test", help="Run offline recorder integration tests")
    sub.add_parser("ai-self-test", help="Run offline AI queue/prediction tests")
    diagnose = sub.add_parser(
        "diagnose", help="Read-only live IBKR connection/provider diagnostic"
    )
    diagnose.add_argument("--seconds", type=int, default=20)
    add_ibkr_option_snapshot_commands(sub)
    add_ibkr_broker_evidence_commands(sub)
    add_market_data_collection_commands(sub)
    add_training_commands(sub)
    add_market_evidence_gate_commands(sub)
    add_core_data_commands(sub)
    db_audit = sub.add_parser("db-audit", help="Run a read-only SQLite/data-quality audit")
    db_audit.add_argument("--strict", action="store_true", help="Exit 2 if critical/error findings exist")
    sub.add_parser("db-repair", help="Reapply reaction governance and rebuild derived event clusters")
    db_backup = sub.add_parser("db-backup", help="Create a consistent VACUUM INTO snapshot of the database and prune old ones")
    db_backup.add_argument("--backup-dir", default="backups", help="Directory to write timestamped backup files into")
    db_backup.add_argument("--keep", type=int, default=14, help="Number of most-recent backups to retain")
    add_operational_admin_commands(sub)
    add_capital_commands(sub)
    add_strategy_allocation_commands(sub)

    init_env = sub.add_parser("init-env", help="Create a .env from .env.example")
    init_env.add_argument("--path", type=Path, default=Path(".env"))

    from prediction_evaluation.cli import add_subcommands
    add_subcommands(sub)
    return p


def main() -> None:
    args = parser().parse_args()
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    dispatch_command(args, settings, globals())


if __name__ == "__main__":
    main()
