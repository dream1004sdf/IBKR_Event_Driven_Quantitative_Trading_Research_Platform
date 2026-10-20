from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recorder.cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
)
from recorder.cli_replay_score_runtime import _parse_float_grid
from recorder.cli_runtime_utils import _run_singleton
from recorder.config import Settings


def command_live_scorer_learning(
    settings: Settings,
    profile_json_path: Path | None,
    database_path: Path | None,
    market_data_json: Path | None,
    evidence_freshness_json: Path | None,
    source_online_supervisor_json: Path | None,
    options_market_structure_json: Path | None,
    options_market_structure_supervisor_json: Path | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    lookback_minutes: int | None,
    max_new_events_per_tick: int | None,
    horizons_minutes: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.live_scorer_learning import (
        LiveScorerLearningConfig,
        live_scorer_learning_markdown,
        run_live_scorer_learning,
        write_live_scorer_learning_json,
        write_live_scorer_learning_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    def _horizons(value: str | None) -> tuple[int, ...]:
        raw = value or str(_coalesce_profile_value(None, profile, "learning", "horizons_minutes", "5,15,30"))
        return tuple(int(item.strip()) for item in str(raw).split(",") if item.strip())

    config = LiveScorerLearningConfig(
        database_path=_coalesce_profile_path(database_path, profile, "paths", "database_path", profile_base) or settings.database_path,
        market_data_json_path=_coalesce_profile_path(market_data_json, profile, "paths", "market_data_json", profile_base),
        evidence_freshness_json_path=_coalesce_profile_path(evidence_freshness_json, profile, "paths", "evidence_freshness_json", profile_base),
        source_online_supervisor_json_path=_coalesce_profile_path(source_online_supervisor_json, profile, "paths", "source_online_supervisor_json", profile_base),
        options_market_structure_json_path=_coalesce_profile_path(options_market_structure_json, profile, "paths", "options_market_structure_json", profile_base),
        options_market_structure_supervisor_json_path=_coalesce_profile_path(options_market_structure_supervisor_json, profile, "paths", "options_market_structure_supervisor_json", profile_base),
        m232_hot_cache_json_path=_coalesce_profile_path(None, profile, "paths", "m232_hot_cache_json", profile_base),
        max_ticks=int(_coalesce_profile_value(max_ticks, profile, "learning", "max_ticks", 1)),
        interval_seconds=float(_coalesce_profile_value(interval_seconds, profile, "learning", "interval_seconds", 1.0)),
        sleep_between_ticks=bool(_coalesce_profile_value(sleep_between_ticks, profile, "learning", "sleep_between_ticks", False)),
        lookback_minutes=int(_coalesce_profile_value(lookback_minutes, profile, "learning", "lookback_minutes", 180)),
        max_new_events_per_tick=int(_coalesce_profile_value(max_new_events_per_tick, profile, "learning", "max_new_events_per_tick", 25)),
        horizons_minutes=_horizons(horizons_minutes),
        scorer_name=str(_coalesce_profile_value(None, profile, "learning", "scorer_name", "m223_shadow_heuristic")),
        scorer_version=str(_coalesce_profile_value(None, profile, "learning", "scorer_version", "shadow-threshold-candidate-v1")),
        magnitude_threshold_pct=float(_coalesce_profile_value(None, profile, "learning", "magnitude_threshold_pct", 0.15)),
        output_path=_coalesce_profile_path(output, profile, "outputs", "output", profile_base),
        json_output_path=_coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base),
    )
    result = _run_singleton(
        "live-scorer-learning",
        lambda: run_live_scorer_learning(config),
    )
    if result.get("status") == "skipped_already_running":
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{result.get('command')} skipped: {result.get('reason')} ({(result.get('singleton') or {}).get('message')})")
        return result
    written = write_live_scorer_learning_markdown(config.output_path, result) if config.output_path else None
    json_written = write_live_scorer_learning_json(config.json_output_path, result) if config.json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(live_scorer_learning_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_live_learning_llm_notes(
    settings: Settings,
    profile_json_path: Path | None,
    database_path: Path | None,
    openai_model: str | None,
    limit: int | None,
    min_packet_quality_score: float | None,
    min_note_confidence: float | None,
    min_reason_summary_chars: int | None,
    min_evidence_weight_total: float | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    cooldown_seconds: float | None,
    max_consecutive_errors: int | None,
    adaptive_backlog: bool | None,
    adaptive_min_limit: int | None,
    adaptive_max_limit: int | None,
    adaptive_low_backlog: int | None,
    adaptive_high_backlog: int | None,
    adaptive_critical_backlog: int | None,
    adaptive_min_interval_seconds: float | None,
    adaptive_max_interval_seconds: float | None,
    state_json_path: Path | None,
    focus_feedback_json_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    dry_run: bool,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.live_learning_llm_notes import (
        LiveLearningLLMNoteConfig,
        live_learning_llm_notes_markdown,
        run_live_learning_llm_adjustment_notes,
        write_live_learning_llm_notes_json,
        write_live_learning_llm_notes_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    config = LiveLearningLLMNoteConfig(
        database_path=_coalesce_profile_path(database_path, profile, "paths", "database_path", profile_base) or settings.database_path,
        api_key=settings.openai_api_key,
        model=str(_coalesce_profile_value(openai_model, profile, "llm_notes", "openai_model", settings.ai_model)),
        limit=int(_coalesce_profile_value(limit, profile, "llm_notes", "limit", 10)),
        min_packet_quality_score=float(_coalesce_profile_value(min_packet_quality_score, profile, "llm_notes", "min_packet_quality_score", 40.0)),
        min_note_confidence=float(_coalesce_profile_value(min_note_confidence, profile, "llm_notes", "min_note_confidence", 0.55)),
        min_reason_summary_chars=int(_coalesce_profile_value(min_reason_summary_chars, profile, "llm_notes", "min_reason_summary_chars", 80)),
        min_evidence_weight_total=float(_coalesce_profile_value(min_evidence_weight_total, profile, "llm_notes", "min_evidence_weight_total", 0.75)),
        max_ticks=int(_coalesce_profile_value(max_ticks, profile, "llm_notes", "max_ticks", 1)),
        interval_seconds=float(_coalesce_profile_value(interval_seconds, profile, "llm_notes", "interval_seconds", 300.0)),
        sleep_between_ticks=bool(_coalesce_profile_value(sleep_between_ticks, profile, "llm_notes", "sleep_between_ticks", False)),
        cooldown_seconds=float(_coalesce_profile_value(cooldown_seconds, profile, "llm_notes", "cooldown_seconds", 300.0)),
        max_consecutive_errors=int(_coalesce_profile_value(max_consecutive_errors, profile, "llm_notes", "max_consecutive_errors", 3)),
        adaptive_backlog_enabled=bool(_coalesce_profile_value(adaptive_backlog, profile, "llm_notes", "adaptive_backlog_enabled", False)),
        adaptive_min_limit=int(_coalesce_profile_value(adaptive_min_limit, profile, "llm_notes", "adaptive_min_limit", 3)),
        adaptive_max_limit=int(_coalesce_profile_value(adaptive_max_limit, profile, "llm_notes", "adaptive_max_limit", 25)),
        adaptive_low_backlog=int(_coalesce_profile_value(adaptive_low_backlog, profile, "llm_notes", "adaptive_low_backlog", 25)),
        adaptive_high_backlog=int(_coalesce_profile_value(adaptive_high_backlog, profile, "llm_notes", "adaptive_high_backlog", 100)),
        adaptive_critical_backlog=int(_coalesce_profile_value(adaptive_critical_backlog, profile, "llm_notes", "adaptive_critical_backlog", 300)),
        adaptive_min_interval_seconds=float(_coalesce_profile_value(adaptive_min_interval_seconds, profile, "llm_notes", "adaptive_min_interval_seconds", 120.0)),
        adaptive_max_interval_seconds=float(_coalesce_profile_value(adaptive_max_interval_seconds, profile, "llm_notes", "adaptive_max_interval_seconds", 900.0)),
        note_version=str(_coalesce_profile_value(None, profile, "llm_notes", "note_version", "m225-llm-adjustment-notes-v1")),
        output_path=_coalesce_profile_path(output, profile, "outputs", "output", profile_base),
        json_output_path=_coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base),
        state_json_path=_coalesce_profile_path(state_json_path, profile, "outputs", "state_json_path", profile_base),
        focus_feedback_json_path=_coalesce_profile_path(focus_feedback_json_path, profile, "outputs", "focus_feedback_json_path", profile_base),
        dry_run=bool(dry_run or _coalesce_profile_value(None, profile, "llm_notes", "dry_run", False)),
    )
    result = _run_singleton(
        "live-learning-llm-notes",
        lambda: run_live_learning_llm_adjustment_notes(config),
    )
    if result.get("status") == "skipped_already_running":
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{result.get('command')} skipped: {result.get('reason')} ({(result.get('singleton') or {}).get('message')})")
        return result
    if config.output_path:
        write_live_learning_llm_notes_markdown(config.output_path, result)
    if config.json_output_path:
        write_live_learning_llm_notes_json(config.json_output_path, result)
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(live_learning_llm_notes_markdown(result))
    return result


def command_threshold_candidate_evaluator(
    settings: Settings,
    profile_json_path: Path | None,
    database_path: Path | None,
    min_watch_sample_count: int | None,
    min_promotion_sample_count: int | None,
    min_avg_confidence: float | None,
    min_avg_note_quality_score: float | None,
    min_promotion_score: float | None,
    min_estimated_error_reduction_bps: float | None,
    max_candidates: int | None,
    semantic_family: bool | None,
    outcome_direction_split: bool | None,
    max_focus_patterns: int | None,
    shadow_promotion_trial: bool | None,
    min_shadow_trial_cycles: int | None,
    min_shadow_acceptance_score: float | None,
    promotion_gate: bool | None,
    promotion_gate_min_sample_count: int | None,
    promotion_gate_recent_trial_count: int | None,
    promotion_gate_min_recent_acceptance_score: float | None,
    promotion_gate_max_abs_error_worsening_pct: float | None,
    promotion_gate_max_regime_family_share: float | None,
    promotion_gate_watchlist: bool | None,
    promotion_gate_watchlist_stale_ticks: int | None,
    promotion_gate_watchlist_recycle_ticks: int | None,
    promotion_gate_recycle_min_new_samples: int | None,
    promotion_gate_recycle_return_confidence_ticks: int | None,
    promotion_gate_recycle_quarantine_ticks: int | None,
    promotion_gate_recycle_dormant_ticks: int | None,
    promotion_gate_recycle_retire_ticks: int | None,
    promotion_gate_recycle_near_return_remaining_checks: int | None,
    promotion_gate_option_root_cause_policy: bool | None,
    promotion_gate_transition_history_limit: int | None,
    evidence_freshness_json_path: Path | None,
    options_market_structure_json_path: Path | None,
    options_market_structure_supervisor_json_path: Path | None,
    portfolio_risk_state_json_path: Path | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    adaptive_refresh: bool | None,
    adaptive_min_interval_seconds: float | None,
    adaptive_max_interval_seconds: float | None,
    adaptive_high_candidate_count: int | None,
    adaptive_changed_interval_seconds: float | None,
    adaptive_unchanged_interval_seconds: float | None,
    state_json_path: Path | None,
    output: Path | None,
    json_output_path: Path | None,
    dry_run: bool,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.threshold_candidate_evaluator import (
        ThresholdCandidateEvaluatorConfig,
        evaluate_threshold_candidates,
        threshold_candidate_evaluator_markdown,
        write_threshold_candidate_evaluator_json,
        write_threshold_candidate_evaluator_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    config = ThresholdCandidateEvaluatorConfig(
        database_path=_coalesce_profile_path(database_path, profile, "paths", "database_path", profile_base) or settings.database_path,
        min_watch_sample_count=int(_coalesce_profile_value(min_watch_sample_count, profile, "evaluator", "min_watch_sample_count", 2)),
        min_promotion_sample_count=int(_coalesce_profile_value(min_promotion_sample_count, profile, "evaluator", "min_promotion_sample_count", 8)),
        min_avg_confidence=float(_coalesce_profile_value(min_avg_confidence, profile, "evaluator", "min_avg_confidence", 0.65)),
        min_avg_note_quality_score=float(_coalesce_profile_value(min_avg_note_quality_score, profile, "evaluator", "min_avg_note_quality_score", 70.0)),
        min_promotion_score=float(_coalesce_profile_value(min_promotion_score, profile, "evaluator", "min_promotion_score", 80.0)),
        min_estimated_error_reduction_bps=float(_coalesce_profile_value(min_estimated_error_reduction_bps, profile, "evaluator", "min_estimated_error_reduction_bps", 1.0)),
        max_candidates=int(_coalesce_profile_value(max_candidates, profile, "evaluator", "max_candidates", 100)),
        semantic_family_enabled=bool(_coalesce_profile_value(semantic_family, profile, "evaluator", "semantic_family_enabled", True)),
        outcome_direction_split_enabled=bool(_coalesce_profile_value(outcome_direction_split, profile, "evaluator", "outcome_direction_split_enabled", True)),
        max_focus_patterns=int(_coalesce_profile_value(max_focus_patterns, profile, "evaluator", "max_focus_patterns", 8)),
        shadow_promotion_trial_enabled=bool(_coalesce_profile_value(shadow_promotion_trial, profile, "evaluator", "shadow_promotion_trial_enabled", True)),
        min_shadow_trial_cycles=int(_coalesce_profile_value(min_shadow_trial_cycles, profile, "evaluator", "min_shadow_trial_cycles", 3)),
        min_shadow_acceptance_score=float(_coalesce_profile_value(min_shadow_acceptance_score, profile, "evaluator", "min_shadow_acceptance_score", 75.0)),
        promotion_gate_enabled=bool(_coalesce_profile_value(promotion_gate, profile, "evaluator", "promotion_gate_enabled", True)),
        promotion_gate_min_sample_count=int(_coalesce_profile_value(promotion_gate_min_sample_count, profile, "evaluator", "promotion_gate_min_sample_count", 8)),
        promotion_gate_recent_trial_count=int(_coalesce_profile_value(promotion_gate_recent_trial_count, profile, "evaluator", "promotion_gate_recent_trial_count", 3)),
        promotion_gate_min_recent_acceptance_score=float(_coalesce_profile_value(promotion_gate_min_recent_acceptance_score, profile, "evaluator", "promotion_gate_min_recent_acceptance_score", 75.0)),
        promotion_gate_max_abs_error_worsening_pct=float(_coalesce_profile_value(promotion_gate_max_abs_error_worsening_pct, profile, "evaluator", "promotion_gate_max_abs_error_worsening_pct", 0.25)),
        promotion_gate_max_regime_family_share=float(_coalesce_profile_value(promotion_gate_max_regime_family_share, profile, "evaluator", "promotion_gate_max_regime_family_share", 0.85)),
        promotion_gate_watchlist_enabled=bool(_coalesce_profile_value(promotion_gate_watchlist, profile, "evaluator", "promotion_gate_watchlist_enabled", True)),
        promotion_gate_watchlist_stale_ticks=int(_coalesce_profile_value(promotion_gate_watchlist_stale_ticks, profile, "evaluator", "promotion_gate_watchlist_stale_ticks", 10)),
        promotion_gate_watchlist_recycle_ticks=int(_coalesce_profile_value(promotion_gate_watchlist_recycle_ticks, profile, "evaluator", "promotion_gate_watchlist_recycle_ticks", 20)),
        promotion_gate_recycle_min_new_samples=int(_coalesce_profile_value(promotion_gate_recycle_min_new_samples, profile, "evaluator", "promotion_gate_recycle_min_new_samples", 5)),
        promotion_gate_recycle_return_confidence_ticks=int(_coalesce_profile_value(promotion_gate_recycle_return_confidence_ticks, profile, "evaluator", "promotion_gate_recycle_return_confidence_ticks", 3)),
        promotion_gate_recycle_quarantine_ticks=int(_coalesce_profile_value(promotion_gate_recycle_quarantine_ticks, profile, "evaluator", "promotion_gate_recycle_quarantine_ticks", 3)),
        promotion_gate_recycle_dormant_ticks=int(_coalesce_profile_value(promotion_gate_recycle_dormant_ticks, profile, "evaluator", "promotion_gate_recycle_dormant_ticks", 30)),
        promotion_gate_recycle_retire_ticks=int(_coalesce_profile_value(promotion_gate_recycle_retire_ticks, profile, "evaluator", "promotion_gate_recycle_retire_ticks", 60)),
        promotion_gate_recycle_near_return_remaining_checks=int(_coalesce_profile_value(promotion_gate_recycle_near_return_remaining_checks, profile, "evaluator", "promotion_gate_recycle_near_return_remaining_checks", 1)),
        promotion_gate_option_root_cause_policy_enabled=bool(_coalesce_profile_value(promotion_gate_option_root_cause_policy, profile, "evaluator", "promotion_gate_option_root_cause_policy_enabled", True)),
        promotion_gate_transition_history_limit=int(_coalesce_profile_value(promotion_gate_transition_history_limit, profile, "evaluator", "promotion_gate_transition_history_limit", 50)),
        evidence_freshness_json_path=_coalesce_profile_path(evidence_freshness_json_path, profile, "paths", "evidence_freshness_json_path", profile_base),
        options_market_structure_json_path=_coalesce_profile_path(options_market_structure_json_path, profile, "paths", "options_market_structure_json_path", profile_base),
        options_market_structure_supervisor_json_path=_coalesce_profile_path(options_market_structure_supervisor_json_path, profile, "paths", "options_market_structure_supervisor_json_path", profile_base),
        portfolio_risk_state_json_path=_coalesce_profile_path(portfolio_risk_state_json_path, profile, "paths", "portfolio_risk_state_json_path", profile_base),
        max_ticks=int(_coalesce_profile_value(max_ticks, profile, "evaluator", "max_ticks", 1)),
        interval_seconds=float(_coalesce_profile_value(interval_seconds, profile, "evaluator", "interval_seconds", 300.0)),
        sleep_between_ticks=bool(_coalesce_profile_value(sleep_between_ticks, profile, "evaluator", "sleep_between_ticks", False)),
        adaptive_refresh_enabled=bool(_coalesce_profile_value(adaptive_refresh, profile, "evaluator", "adaptive_refresh_enabled", False)),
        adaptive_min_interval_seconds=float(_coalesce_profile_value(adaptive_min_interval_seconds, profile, "evaluator", "adaptive_min_interval_seconds", 60.0)),
        adaptive_max_interval_seconds=float(_coalesce_profile_value(adaptive_max_interval_seconds, profile, "evaluator", "adaptive_max_interval_seconds", 900.0)),
        adaptive_high_candidate_count=int(_coalesce_profile_value(adaptive_high_candidate_count, profile, "evaluator", "adaptive_high_candidate_count", 100)),
        adaptive_changed_interval_seconds=float(_coalesce_profile_value(adaptive_changed_interval_seconds, profile, "evaluator", "adaptive_changed_interval_seconds", 120.0)),
        adaptive_unchanged_interval_seconds=float(_coalesce_profile_value(adaptive_unchanged_interval_seconds, profile, "evaluator", "adaptive_unchanged_interval_seconds", 600.0)),
        state_json_path=_coalesce_profile_path(state_json_path, profile, "outputs", "state_json_path", profile_base),
        output_path=_coalesce_profile_path(output, profile, "outputs", "output", profile_base),
        json_output_path=_coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base),
        dry_run=bool(dry_run or _coalesce_profile_value(None, profile, "evaluator", "dry_run", False)),
    )
    result = _run_singleton(
        "threshold-candidate-evaluator",
        lambda: evaluate_threshold_candidates(config),
    )
    if result.get("status") == "skipped_already_running":
        if json_output:
            print(json.dumps(result, indent=2, ensure_ascii=False))
        else:
            print(f"{result.get('command')} skipped: {result.get('reason')} ({(result.get('singleton') or {}).get('message')})")
        return result
    if config.output_path:
        write_threshold_candidate_evaluator_markdown(config.output_path, result)
    if config.json_output_path:
        write_threshold_candidate_evaluator_json(config.json_output_path, result)
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(threshold_candidate_evaluator_markdown(result))
    return result


def command_replay_standing_regime_family_gate_tune(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    signal_limit: int,
    conflict_score_grid: str | None,
    min_family_rows: int,
    min_directional_coverage: float,
    top_n: int,
    limit: int,
    output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_scoring import (
        standing_regime_family_gate_tuning_markdown,
        standing_regime_family_gate_tuning_summary,
        write_standing_regime_family_gate_tuning_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "signal_limit": signal_limit,
        "conflict_score_grid": conflict_score_grid,
        "min_family_rows": min_family_rows,
        "min_directional_coverage": min_directional_coverage,
        "top_n": top_n,
        "limit": limit,
    }
    summary = standing_regime_family_gate_tuning_summary(
        rows,
        signals,
        db,
        conflict_score_grid=_parse_float_grid(conflict_score_grid, (0.40, 0.50, 0.67)),
        min_family_rows=min_family_rows,
        min_directional_coverage=min_directional_coverage,
        top_n=top_n,
    )
    if json_output:
        print(json.dumps({"filters": filters, "summary": summary}, indent=2, ensure_ascii=False))
        return
    if output:
        written = write_standing_regime_family_gate_tuning_markdown(output, summary, filters=filters)
        print(f"Markdown report written: {written}")
        return
    print(standing_regime_family_gate_tuning_markdown(summary, filters=filters))


def command_walk_forward_learning_sim(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    signal_limit: int,
    min_pattern_observations: int,
    weak_hit_rate: float,
    limit: int,
    output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.walk_forward_training import (
        walk_forward_learning_markdown,
        walk_forward_learning_simulation,
        write_walk_forward_learning_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "signal_limit": signal_limit,
        "min_pattern_observations": min_pattern_observations,
        "weak_hit_rate": weak_hit_rate,
        "limit": limit,
    }
    result = walk_forward_learning_simulation(
        rows,
        signals,
        db,
        min_pattern_observations=min_pattern_observations,
        weak_hit_rate=weak_hit_rate,
    )
    if json_output:
        print(json.dumps({"filters": filters, "summary": result}, indent=2, ensure_ascii=False))
        return
    if output:
        written = write_walk_forward_learning_markdown(output, result, filters=filters)
        print(f"Markdown report written: {written}")
        return
    print(walk_forward_learning_markdown(result, filters=filters))


def command_walk_forward_adjustment_notes(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    signal_limit: int,
    min_pattern_observations: int,
    weak_hit_rate: float,
    min_abs_error: float | None,
    include_hits: bool,
    analysis_provider: str,
    openai_model: str | None,
    reasoning_effort: str,
    max_input_chars: int,
    limit: int,
    score_limit: int,
    output: Path | None,
    json_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ai_provider import PermanentAnalysisError, TransientAnalysisError
    from recorder.analysis_database import AnalysisDatabase
    from recorder.walk_forward_notes import (
        OpenAIWalkForwardNoteAnalyzer,
        build_walk_forward_adjustment_notes,
        walk_forward_adjustment_notes_markdown,
        write_walk_forward_adjustment_notes_json,
        write_walk_forward_adjustment_notes_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=score_limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    normalized_provider = analysis_provider.strip().lower()
    analyzer = None
    if normalized_provider == "openai":
        analyzer = OpenAIWalkForwardNoteAnalyzer(
            api_key=settings.openai_api_key or "",
            model=openai_model or settings.ai_model,
            timeout_seconds=settings.ai_api_timeout_seconds,
            reasoning_effort=reasoning_effort,
            max_input_chars=max_input_chars,
        )
    elif normalized_provider != "deterministic":
        raise SystemExit(f"Unsupported walk-forward note analysis provider: {analysis_provider}")
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "signal_limit": signal_limit,
        "min_pattern_observations": min_pattern_observations,
        "weak_hit_rate": weak_hit_rate,
        "min_abs_error": min_abs_error,
        "include_hits": include_hits,
        "analysis_provider": normalized_provider,
        "limit": limit,
        "score_limit": score_limit,
    }
    try:
        result = build_walk_forward_adjustment_notes(
            rows,
            signals,
            db,
            limit=limit,
            min_pattern_observations=min_pattern_observations,
            weak_hit_rate=weak_hit_rate,
            min_abs_error=min_abs_error,
            include_hits=include_hits,
            analyzer=analyzer,
        )
    except PermanentAnalysisError as exc:
        raise SystemExit(f"Walk-forward note configuration error: {exc}") from exc
    except TransientAnalysisError as exc:
        raise SystemExit(f"Walk-forward note transient error: {exc}") from exc
    written = write_walk_forward_adjustment_notes_markdown(output, result, filters=filters) if output else None
    json_written = write_walk_forward_adjustment_notes_json(json_path, result) if json_path else None
    if json_output:
        print(json.dumps({"filters": filters, "summary": result, "output": str(written) if written else None, "json_output": str(json_written) if json_written else None}, indent=2, ensure_ascii=False))
        return
    if written:
        print(f"Markdown report written: {written}")
        if json_written:
            print(f"JSON notes written: {json_written}")
        return
    print(walk_forward_adjustment_notes_markdown(result, filters=filters))
    if json_written:
        print(f"\nJSON notes written: {json_written}")


def command_walk_forward_cycle_train(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    signal_limit: int,
    cycles: int,
    min_pattern_observations: int,
    weak_hit_rate: float,
    max_threshold_entries: int,
    score_limit: int,
    output: Path | None,
    json_path: Path | None,
    threshold_output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.walk_forward_cycles import (
        run_walk_forward_training_cycles,
        walk_forward_training_cycles_markdown,
        write_threshold_table_json,
        write_walk_forward_training_cycles_json,
        write_walk_forward_training_cycles_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=score_limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "signal_limit": signal_limit,
        "cycles": cycles,
        "min_pattern_observations": min_pattern_observations,
        "weak_hit_rate": weak_hit_rate,
        "max_threshold_entries": max_threshold_entries,
        "score_limit": score_limit,
    }
    result = run_walk_forward_training_cycles(
        rows,
        signals,
        db,
        cycles=cycles,
        min_pattern_observations=min_pattern_observations,
        weak_hit_rate=weak_hit_rate,
        max_threshold_entries=max_threshold_entries,
    )
    written = write_walk_forward_training_cycles_markdown(output, result, filters=filters) if output else None
    json_written = write_walk_forward_training_cycles_json(json_path, result) if json_path else None
    table_written = write_threshold_table_json(threshold_output, result) if threshold_output else None
    if json_output:
        print(
            json.dumps(
                {
                    "filters": filters,
                    "summary": result,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                    "threshold_output": str(table_written) if table_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if written:
        print(f"Markdown report written: {written}")
        if json_written:
            print(f"JSON cycle result written: {json_written}")
        if table_written:
            print(f"Threshold table written: {table_written}")
        return
    print(walk_forward_training_cycles_markdown(result, filters=filters))
    if json_written:
        print(f"\nJSON cycle result written: {json_written}")
    if table_written:
        print(f"\nThreshold table written: {table_written}")


def command_walk_forward_cycle_threshold_tune(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    signal_limit: int,
    cycles: int,
    min_pattern_observations_grid: str,
    weak_hit_rate_grid: str,
    max_threshold_entries_grid: str,
    min_coverage: float,
    coverage_weight: float,
    abs_error_weight: float,
    top_n: int,
    score_limit: int,
    checkpoint_jsonl_path: Path | None,
    resume_checkpoint: bool,
    packet_checkpoint_interval: int,
    output: Path | None,
    json_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.walk_forward_cycles import (
        tune_walk_forward_cycle_thresholds,
        walk_forward_threshold_tuning_markdown,
        write_walk_forward_threshold_tuning_json,
        write_walk_forward_threshold_tuning_markdown,
    )

    def parse_ints(text: str) -> tuple[int, ...]:
        return tuple(int(item.strip()) for item in text.split(",") if item.strip())

    def parse_floats(text: str) -> tuple[float, ...]:
        return tuple(float(item.strip()) for item in text.split(",") if item.strip())

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=score_limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "signal_limit": signal_limit,
        "cycles": cycles,
        "min_pattern_observations_grid": min_pattern_observations_grid,
        "weak_hit_rate_grid": weak_hit_rate_grid,
        "max_threshold_entries_grid": max_threshold_entries_grid,
        "min_coverage": min_coverage,
        "coverage_weight": coverage_weight,
        "abs_error_weight": abs_error_weight,
        "top_n": top_n,
        "score_limit": score_limit,
    }
    result = tune_walk_forward_cycle_thresholds(
        rows,
        signals,
        db,
        cycles=cycles,
        min_pattern_observations_grid=parse_ints(min_pattern_observations_grid),
        weak_hit_rate_grid=parse_floats(weak_hit_rate_grid),
        max_threshold_entries_grid=parse_ints(max_threshold_entries_grid),
        min_coverage=min_coverage,
        coverage_weight=coverage_weight,
        abs_error_weight=abs_error_weight,
        top_n=top_n,
        checkpoint_jsonl_path=checkpoint_jsonl_path,
        resume_from_checkpoint=resume_checkpoint,
        packet_checkpoint_interval=packet_checkpoint_interval,
    )
    written = write_walk_forward_threshold_tuning_markdown(output, result, filters=filters) if output else None
    json_written = write_walk_forward_threshold_tuning_json(json_path, result) if json_path else None
    if json_output:
        print(
            json.dumps(
                {
                    "filters": filters,
                    "summary": result,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if written:
        print(f"Markdown report written: {written}")
        if json_written:
            print(f"JSON tuning result written: {json_written}")
        return
    print(walk_forward_threshold_tuning_markdown(result, filters=filters))
    if json_written:
        print(f"\nJSON tuning result written: {json_written}")


def command_walk_forward_cycle_nuanced_train(
    settings: Settings,
    event_type: str | None,
    symbols: str | None,
    price_source: str | None,
    horizon: str | None,
    scorer_name: str | None,
    model_name: str | None,
    scorer_version: str | None,
    start: str | None,
    end: str | None,
    signal_limit: int,
    cycles: int,
    min_pattern_observations: int,
    reflex_hit_rate: float,
    second_pass_hit_rate: float,
    haircut_hit_rate: float,
    neutral_flat_rate: float,
    max_threshold_entries: int,
    score_limit: int,
    output: Path | None,
    json_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.walk_forward_cycles import (
        run_walk_forward_nuanced_action_cycles,
        walk_forward_nuanced_actions_markdown,
        write_walk_forward_nuanced_actions_json,
        write_walk_forward_nuanced_actions_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    symbol_tuple = tuple(item.strip().upper() for item in (symbols or "").split(",") if item.strip()) or None
    rows = db.replay_model_score_analysis_rows(
        event_type=event_type,
        symbols=symbol_tuple,
        price_source=price_source,
        horizon=horizon,
        scorer_name=scorer_name,
        model_name=model_name,
        scorer_version=scorer_version,
        start=start,
        end=end,
        limit=score_limit,
    )
    signals = db.recent_external_market_signals(limit=signal_limit) if hasattr(db, "recent_external_market_signals") else []
    filters = {
        "event_type": event_type,
        "symbols": ",".join(symbol_tuple) if symbol_tuple else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "scorer_name": scorer_name,
        "model_name": model_name,
        "scorer_version": scorer_version,
        "start": start,
        "end": end,
        "signal_limit": signal_limit,
        "cycles": cycles,
        "min_pattern_observations": min_pattern_observations,
        "reflex_hit_rate": reflex_hit_rate,
        "second_pass_hit_rate": second_pass_hit_rate,
        "haircut_hit_rate": haircut_hit_rate,
        "neutral_flat_rate": neutral_flat_rate,
        "max_threshold_entries": max_threshold_entries,
        "score_limit": score_limit,
    }
    result = run_walk_forward_nuanced_action_cycles(
        rows,
        signals,
        db,
        cycles=cycles,
        min_pattern_observations=min_pattern_observations,
        reflex_hit_rate=reflex_hit_rate,
        second_pass_hit_rate=second_pass_hit_rate,
        haircut_hit_rate=haircut_hit_rate,
        neutral_flat_rate=neutral_flat_rate,
        max_threshold_entries=max_threshold_entries,
    )
    written = write_walk_forward_nuanced_actions_markdown(output, result, filters=filters) if output else None
    json_written = write_walk_forward_nuanced_actions_json(json_path, result) if json_path else None
    if json_output:
        print(
            json.dumps(
                {
                    "filters": filters,
                    "summary": result,
                    "output": str(written) if written else None,
                    "json_output": str(json_written) if json_written else None,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    if written:
        print(f"Markdown report written: {written}")
        if json_written:
            print(f"JSON nuanced result written: {json_written}")
        return
    print(walk_forward_nuanced_actions_markdown(result, filters=filters))
    if json_written:
        print(f"\nJSON nuanced result written: {json_written}")
