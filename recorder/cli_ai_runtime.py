from __future__ import annotations

import json

from recorder.config import Settings


def command_ai_self_test() -> None:
    from recorder.ai_self_test import run_ai_self_test

    result = run_ai_self_test()
    print(json.dumps(result, indent=2, ensure_ascii=False))


def _create_ai_service(settings: Settings, **kwargs):
    from recorder.ai_provider import PermanentAnalysisError
    from recorder.analysis_service import AIAnalysisService

    try:
        return AIAnalysisService(settings, **kwargs)
    except PermanentAnalysisError as exc:
        raise SystemExit(f"AI configuration error: {exc}") from exc


def command_ai_run(settings: Settings) -> None:
    service = _create_ai_service(settings)
    service.run_forever()


def command_ai_once(settings: Settings, limit: int) -> None:
    service = _create_ai_service(settings)
    processed = service.run_once(limit=limit)
    print(f"Processed {processed} AI analysis job(s).")


def command_ai_report(settings: Settings, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    print(json.dumps(db.analysis_summary(settings.analysis_key), indent=2, ensure_ascii=False))
    print("\nRecent analyses:")
    for row in db.recent_analyses(limit=limit, analysis_key=settings.analysis_key):
        eligible = "eligible" if row["prediction_eligible"] else "no-prediction"
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            raw = {}
        stage = raw.get("event_stage", "unknown")
        surprise = raw.get("surprise_direction", "unknown")
        depth = raw.get("analysis_depth", "unknown")
        economic_status = raw.get("economic_plausibility_status", "not_checked")
        economic_score = float(raw.get("economic_anomaly_score") or 0.0)
        economic_reference = raw.get("economic_reference_price")
        economic_reference_source = raw.get("economic_reference_price_source") or "-"
        economic_currency = raw.get("economic_currency") or "unknown"
        split_status = raw.get("split_adjustment_status") or "unknown"
        confirmation_count = int(raw.get("source_confirmation_count") or 0)
        display_target = row["primary_symbol"] if row["subject_target_available"] else "-"
        try:
            mentioned = json.loads(row["mentioned_symbols_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            mentioned = []
        delivery_reference = row["delivery_reference_price"]
        _, baseline_rows = db.get_news_context(int(row["news_id"]))
        target_quote_row = next(
            (
                item
                for item in baseline_rows
                if row["subject_target_available"]
                and str(item.get("symbol") or "").upper() == str(row["primary_symbol"]).upper()
            ),
            None,
        )
        if target_quote_row is not None:
            target_quote_audit = (
                f"symbol={target_quote_row.get('symbol') or '-'} "
                f"conId={target_quote_row.get('con_id') or '-'} "
                f"contract={target_quote_row.get('contract_symbol') or '-'} "
                f"secType={target_quote_row.get('sec_type') or '-'} "
                f"exchange={target_quote_row.get('exchange') or '-'} "
                f"primaryExchange={target_quote_row.get('primary_exchange') or '-'} "
                f"marketDataType={target_quote_row.get('market_data_type') if target_quote_row.get('market_data_type') is not None else '-'} "
                f"bid={target_quote_row.get('bid') if target_quote_row.get('bid') is not None else '-'} "
                f"ask={target_quote_row.get('ask') if target_quote_row.get('ask') is not None else '-'} "
                f"last={target_quote_row.get('last') if target_quote_row.get('last') is not None else '-'} "
                f"close={target_quote_row.get('close') if target_quote_row.get('close') is not None else '-'} "
                f"spread={target_quote_row.get('spread_ratio') if target_quote_row.get('spread_ratio') is not None else '-'}"
            )
        else:
            target_quote_audit = "no attributed target quote"
        print(
            f"Analysis #{row['id']} news #{row['news_id']} {row['completed_at']} "
            f"[target={display_target}] scope={row['event_scope']} "
            f"subject={row['subject_entity'] or '-'}({row['event_subject_symbol'] or '-'}) "
            f"asset={row['subject_asset_type']}->{row['target_asset_type']} role={row['target_role']} "
            f"attribution={row['attribution_status']}({row['attribution_confidence']:.2f}) "
            f"subscription={row['original_subscription_symbol'] or '-'} "
            f"{row['event_type']}/{row['event_subtype']} {row['sentiment']} {eligible} "
            f"observe={bool(row['reaction_observation_eligible'])} cluster={row['event_cluster_id'] or '-'} "
            f"predictions={row['prediction_count']}\n"
            f"  model={row['model_name']} prompt={row['prompt_version']} "
            f"stage={stage} surprise={surprise} depth={depth} "
            f"impact={row['impact_score']:.2f} confidence={row['confidence']:.2f} "
            f"source={row['source_tier']}/{row['official_source_status']} "
            f"integrity={row['text_integrity_status']} "
            f"recovery={row['integrity_recovery_status']}"
            f"({row['integrity_recovery_attempts']}:{row['integrity_recovery_model'] or '-'}) "
            f"facts_ok/rejected={row['validated_fact_count']}/{row['rejected_fact_count']}\n"
            f"  attribution_reason={row['attribution_reason'] or '-'}\n"
            f"  economic={economic_status} score={economic_score:.2f} "
            f"reference={economic_reference if economic_reference is not None else '-'}"
            f"({economic_reference_source}) currency={economic_currency} "
            f"split={split_status} confirmations={confirmation_count}\n"
            f"  {row['headline']}\n"
            f"  {row['summary']}"
        )
        facts = raw.get("key_facts") or []
        for fact in facts[:6]:
            if not isinstance(fact, dict):
                continue
            values = []
            for label in ("actual", "expected", "prior"):
                if fact.get(label):
                    values.append(f"{label}={fact[label]}")
            suffix = f" ({', '.join(values)})" if values else ""
            print(
                f"    fact: {fact.get('name', 'unknown')}{suffix} "
                f"direction={fact.get('direction', 'unknown')} "
                f"importance={fact.get('importance', 'medium')}"
            )
        economic_notes = raw.get("economic_validation_notes") or []
        if economic_notes:
            print("    economic checks: " + " | ".join(str(x) for x in economic_notes[:6]))
        limitations = raw.get("data_limitations") or []
        if limitations:
            print("    limitations: " + " | ".join(str(x) for x in limitations[:4]))
        for prediction in db.prediction_rows(int(row["id"])):
            status = prediction["evaluation_status"]
            reason = prediction["invalid_reason"] or prediction["quality_reason"]
            status_text = status if not reason else f"{status}: {reason}"
            print(
                f"    {prediction['horizon']} ({prediction['evaluation_label']}): "
                f"expected={prediction['expected_return']:+.2%} "
                f"range=[{prediction['bear_return']:+.2%}, {prediction['bull_return']:+.2%}] "
                f"P(up/flat/down)={prediction['up_probability']:.0%}/"
                f"{prediction['flat_probability']:.0%}/{prediction['down_probability']:.0%} "
                f"confidence={prediction['confidence']:.0%} {status_text} "
                f"directional={bool(prediction['valid_for_directional_trading'])} "
                f"calibration={bool(prediction['valid_for_probability_calibration'])}"
            )


def command_ai_retry_dead(settings: Settings, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    count = db.reset_dead_jobs(settings.analysis_key, limit=limit)
    print(f"Reset {count} dead job(s) to retry.")


def command_ai_diagnose(settings: Settings) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    print(
        json.dumps(
            {
                "database": str(settings.database_path.resolve()),
                "database_exists": settings.database_path.exists(),
                "ai_provider": settings.ai_provider,
                "ai_primary_model": settings.ai_model,
                "ai_critical_model": settings.ai_critical_model
                if settings.ai_enable_critical_model
                else "disabled",
                "primary_reasoning_effort": settings.ai_primary_reasoning_effort,
                "critical_reasoning_effort": settings.ai_critical_reasoning_effort,
                "critical_event_types": settings.ai_critical_event_types,
                "critical_article_body_enabled": settings.ai_critical_use_article_body,
                "prompt_version": settings.ai_prompt_version,
                "api_key_present": bool(settings.openai_api_key)
                if settings.ai_provider == "openai"
                else None,
                "analysis_key": settings.analysis_key,
                "official_source_auto_fetch": settings.official_source_auto_fetch,
                "integrity_recovery": {
                    "enabled": settings.ai_integrity_recovery_enabled,
                    "critical_model": settings.ai_critical_model if settings.ai_enable_critical_model else "disabled",
                    "min_impact": settings.ai_integrity_recovery_min_impact,
                    "min_validated_facts": settings.ai_integrity_recovery_min_validated_facts,
                    "min_high_facts": settings.ai_integrity_recovery_min_high_facts,
                    "warning_reject_ratio": settings.ai_integrity_recovery_warning_reject_ratio,
                    "partial_calibration_only": settings.ai_integrity_recovery_partial_calibration_only,
                },
                "prediction_quality_gate": {
                    "min_analysis_confidence": settings.prediction_min_analysis_confidence,
                    "min_prediction_confidence": settings.prediction_min_confidence,
                    "min_top_probability": settings.prediction_min_top_probability,
                    "min_probability_edge": settings.prediction_min_probability_edge,
                    "official_required_event_types": settings.prediction_official_required_event_types,
                    "min_validated_facts_without_official": settings.prediction_min_validated_facts_without_official,
                },
                "quote_integrity_gate": {
                    "enabled": settings.quote_integrity_enabled,
                    "max_spread_ratio": settings.quote_integrity_max_spread_ratio,
                    "max_last_midpoint_deviation": settings.quote_integrity_max_last_midpoint_deviation,
                    "max_close_reference_deviation": settings.quote_integrity_max_close_reference_deviation,
                    "require_stock_contract": settings.quote_integrity_require_stock_contract,
                },
                "queue": db.analysis_summary(settings.analysis_key),
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    # Constructing the service validates provider/package/API-key configuration
    # without sending an API request or consuming tokens.
    _create_ai_service(settings, db=db)
    print("AI configuration validation: PASS")
