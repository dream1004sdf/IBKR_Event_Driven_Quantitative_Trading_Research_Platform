from __future__ import annotations

from pathlib import Path

def add_replay_dataset_commands(sub):
    replay_corpus_build = sub.add_parser(
        "replay-corpus-build",
        help="Build leakage-audited replay corpus rows from historical events",
    )
    replay_corpus_build.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_corpus_build.add_argument("--start", help="Inclusive ISO timestamp/date lower bound")
    replay_corpus_build.add_argument("--end", help="Exclusive ISO timestamp/date upper bound")
    replay_corpus_build.add_argument("--limit", type=int, default=1000)
    replay_corpus_report = sub.add_parser("replay-corpus-report", help="Show replay corpus rows")
    replay_corpus_report.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_corpus_report.add_argument("--limit", type=int, default=50)
    replay_outcome_build = sub.add_parser(
        "replay-outcome-build",
        help="Attach post-event daily or intraday outcomes to replay corpus rows",
    )
    replay_outcome_build.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_outcome_build.add_argument("--start", help="Inclusive ISO timestamp/date lower bound")
    replay_outcome_build.add_argument("--end", help="Exclusive ISO timestamp/date upper bound")
    replay_outcome_build.add_argument("--horizons", default="1D,5D", help="Comma-separated horizons, e.g. 30M,2H or 1D,5D")
    replay_outcome_build.add_argument("--benchmark", default="SPY", help="Benchmark symbol for excess return")
    replay_outcome_build.add_argument("--price-source", choices=("yahoo", "stooq", "ibkr"), default="yahoo")
    replay_outcome_build.add_argument("--ibkr-client-id", type=int, help="Override IBKR historical-data clientId")
    replay_outcome_build.add_argument("--ibkr-timeout", type=float, default=20.0)
    replay_outcome_build.add_argument("--ibkr-what-to-show", default="TRADES")
    replay_outcome_build.add_argument("--ibkr-bar-size", default="5 mins", help="IBKR intraday bar size, e.g. '1 min' or '5 mins'")
    replay_outcome_build.add_argument("--ibkr-all-sessions", action="store_true", help="Request all sessions instead of regular trading hours")
    replay_outcome_build.add_argument("--limit", type=int, default=1000)
    replay_outcome_report = sub.add_parser("replay-outcome-report", help="Show replay market outcome rows")
    replay_outcome_report.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_outcome_report.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_outcome_report.add_argument("--limit", type=int, default=50)
    replay_outcome_compare = sub.add_parser(
        "replay-outcome-compare",
        help="Compare stored replay outcomes across price sources and horizons",
    )
    replay_outcome_compare.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_outcome_compare.add_argument("--symbol", help="Ticker symbol to filter")
    replay_outcome_compare.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_outcome_compare.add_argument("--limit", type=int, default=10)
    replay_outcome_compare.add_argument("--output", type=Path, help="Optional comparison report output path")
    replay_outcome_compare.add_argument("--json", action="store_true", help="Print machine-readable comparison JSON")
    replay_dataset_export = sub.add_parser(
        "replay-dataset-export",
        help="Export leakage-checked replay inputs joined to separated outcome labels",
    )
    replay_dataset_export.add_argument("--output", type=Path, help="Output CSV or JSONL path; defaults to exports/")
    replay_dataset_export.add_argument("--format", choices=("csv", "jsonl"), default="csv")
    replay_dataset_export.add_argument("--event-type", choices=("earnings", "guidance", "earnings_guidance"))
    replay_dataset_export.add_argument("--symbol", help="Ticker symbol to filter")
    replay_dataset_export.add_argument("--price-source", help="Exact stored price_source to filter")
    replay_dataset_export.add_argument("--horizon", help="Outcome horizon to filter, e.g. 30M, 2H, 1D, 5D")
    replay_dataset_export.add_argument("--start", help="Inclusive replay timestamp/date lower bound")
    replay_dataset_export.add_argument("--end", help="Exclusive replay timestamp/date upper bound")
    replay_dataset_export.add_argument("--limit", type=int, default=1000)
    replay_dataset_export.add_argument("--include-blocked", action="store_true", help="Include blocked/incomplete outcome rows")
    replay_dataset_export.add_argument(
        "--include-input-snapshot",
        action="store_true",
        help="Include full point-in-time replay input snapshot in JSONL records",
    )
    news_outcome_backfill = sub.add_parser(
        "news-market-outcome-backfill",
        help="Backfill per-news market outcomes for stored news/symbol links into market_reactions",
    )
    news_outcome_backfill.add_argument("--start", required=True, help="Inclusive news published_at lower bound")
    news_outcome_backfill.add_argument("--end", required=True, help="Inclusive news published_at upper bound")
    news_outcome_backfill.add_argument("--symbols", help="Comma-separated ticker symbols to include")
    news_outcome_backfill.add_argument(
        "--horizons",
        default="NEXT_DAY",
        help="Comma-separated horizons: 5M,15M,1H,CLOSE,NEXT_DAY",
    )
    news_outcome_backfill.add_argument("--price-source", choices=("yahoo", "ibkr"), default="yahoo")
    news_outcome_backfill.add_argument("--limit", type=int, default=1000)
    news_outcome_backfill.add_argument("--max-groups", type=int, default=0, help="Intraday symbol/day group cap; 0 means no cap")
    news_outcome_backfill.add_argument("--only-missing", dest="only_missing", action="store_true", help="Only build missing news/symbol/horizon reactions; existing market_reactions rows are skipped")
    news_outcome_backfill.add_argument("--include-existing", dest="only_missing", action="store_false", help="Rebuild even when a matching market_reactions row already exists")
    news_outcome_backfill.set_defaults(only_missing=True)
    news_outcome_backfill.add_argument("--include-pseudo-symbols", action="store_true", help="Include pseudo symbols such as BROADTAPE:BZ")
    news_outcome_backfill.add_argument("--flat-threshold", type=float, default=0.001)
    news_outcome_backfill.add_argument("--symbol-tier-config", type=Path, help="Optional symbol tier config for priority/event-window filtering")
    news_outcome_backfill.add_argument("--symbol-tier-filter", help="Comma-separated tiers to include, e.g. core_always,macro_proxy")
    news_outcome_backfill.add_argument(
        "--event-proxy-policy",
        choices=("matched-news-only", "full-symbol"),
        default="matched-news-only",
        help="For event-window tiers, include only matched news by default or the full symbol history",
    )
    news_outcome_backfill.add_argument(
        "--unknown-symbol-policy",
        choices=("exclude", "include"),
        default="exclude",
        help="Whether symbols missing from the tier config are eligible when tier filtering is active",
    )
    news_outcome_backfill.add_argument("--ibkr-client-id", type=int)
    news_outcome_backfill.add_argument("--ibkr-timeout", type=float, default=20.0)
    news_outcome_backfill.add_argument("--ibkr-what-to-show", default="TRADES")
    news_outcome_backfill.add_argument("--ibkr-bar-size", default="1 min")
    news_outcome_backfill.add_argument("--ibkr-all-sessions", action="store_true")
    news_outcome_backfill.add_argument(
        "--close-retry-bypass-after-failures",
        type=int,
        default=3,
        help="After this many consecutive close-retry failures for a symbol, skip wide retry and use daily CLOSE fallback; 0 disables",
    )
    news_outcome_backfill.add_argument("--progress-jsonl-path", type=Path, default=Path("logs/news_market_outcome_backfill_progress.jsonl"))
    news_outcome_backfill.add_argument("--output", type=Path, default=Path("exports/news_market_outcome_backfill.md"))
    news_outcome_backfill.add_argument("--json-output-path", type=Path, default=Path("exports/news_market_outcome_backfill.json"))
    news_outcome_backfill.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    news_symbol_tier_report = sub.add_parser(
        "news-symbol-tier-report",
        help="Classify stored news-linked symbols into core, event-proxy, opportunistic, and review tiers",
    )
    news_symbol_tier_report.add_argument("--start", required=True)
    news_symbol_tier_report.add_argument("--end", required=True)
    news_symbol_tier_report.add_argument("--symbol-tier-config", type=Path, default=Path("config/news_market_outcome_symbol_tiers_v1.json"))
    news_symbol_tier_report.add_argument("--limit-samples-per-symbol", type=int, default=2)
    news_symbol_tier_report.add_argument("--output", type=Path, default=Path("exports/news_symbol_tier_report.md"))
    news_symbol_tier_report.add_argument("--json-output-path", type=Path, default=Path("exports/news_symbol_tier_report.json"))
    news_symbol_tier_report.add_argument("--json", action="store_true")

    clean_cycle_audit = sub.add_parser(
        "clean-backfill-cycle-audit",
        help="Audit a clean backfill/training cycle and record whether Codex intervention is allowed",
    )
    clean_cycle_audit.add_argument("--start", required=True)
    clean_cycle_audit.add_argument("--end", required=True)
    clean_cycle_audit.add_argument("--cycle-index", type=int, default=1)
    clean_cycle_audit.add_argument(
        "--cycle-mode",
        choices=("auto", "codex-assisted", "blind", "source-locked-blind-reclass"),
        default="auto",
        help="auto means cycle 1 is Codex-assisted; cycle 2+ is forced to blind; source-locked-blind-reclass reruns derived classification on fixed raw sources",
    )
    clean_cycle_audit.add_argument("--phase", default="evidence_conversion")
    clean_cycle_audit.add_argument("--symbol-tier-config", type=Path, default=Path("config/news_market_outcome_symbol_tiers_v1.json"))
    clean_cycle_audit.add_argument("--correction-ledger-path", type=Path, default=Path("exports/clean_backfill_cycle_correction_ledger.jsonl"))
    clean_cycle_audit.add_argument("--output", type=Path, default=Path("exports/clean_backfill_cycle_audit.md"))
    clean_cycle_audit.add_argument("--json-output-path", type=Path, default=Path("exports/clean_backfill_cycle_audit.json"))
    clean_cycle_audit.add_argument("--json", action="store_true")

    clean_cycle_report = sub.add_parser(
        "clean-backfill-cycle-report",
        help="Summarize clean backfill cycle accuracy/PnL artifacts and next-cycle blind mode",
    )
    clean_cycle_report.add_argument("--start", required=True)
    clean_cycle_report.add_argument("--end", required=True)
    clean_cycle_report.add_argument("--cycle-index", type=int, default=1)
    clean_cycle_report.add_argument(
        "--cycle-mode",
        choices=("auto", "codex-assisted", "blind", "source-locked-blind-reclass"),
        default="auto",
        help="auto means cycle 1 is Codex-assisted; cycle 2+ is forced to blind; source-locked-blind-reclass reruns derived classification on fixed raw sources",
    )
    clean_cycle_report.add_argument("--artifact-dir", type=Path, default=Path("exports"))
    clean_cycle_report.add_argument("--correction-ledger-path", type=Path, default=Path("exports/clean_backfill_cycle_correction_ledger.jsonl"))
    clean_cycle_report.add_argument("--output", type=Path, default=Path("exports/clean_backfill_cycle_report.md"))
    clean_cycle_report.add_argument("--json-output-path", type=Path, default=Path("exports/clean_backfill_cycle_report.json"))
    clean_cycle_report.add_argument("--json", action="store_true")

    signal_audit = sub.add_parser(
        "clean-signal-classification-audit",
        help="Audit external_market_signals classification quality and write notes for M236/source-quality learning",
    )
    signal_audit.add_argument("--start", required=True)
    signal_audit.add_argument("--end", required=True)
    signal_audit.add_argument("--limit", type=int, default=100000)
    signal_audit.add_argument("--symbol-tier-report-path", type=Path, default=Path("exports/historical_backfill_news_symbol_tiers.json"))
    signal_audit.add_argument("--output", type=Path, default=Path("exports/clean_backfill_signal_classification_audit.md"))
    signal_audit.add_argument("--json-output-path", type=Path, default=Path("exports/clean_backfill_signal_classification_audit.json"))
    signal_audit.add_argument("--notes-jsonl-path", type=Path, default=Path("exports/clean_backfill_signal_classification_notes.jsonl"))
    signal_audit.add_argument("--json", action="store_true")
