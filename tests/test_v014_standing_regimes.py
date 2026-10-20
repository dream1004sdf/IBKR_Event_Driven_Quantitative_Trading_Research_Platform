from __future__ import annotations

from types import SimpleNamespace

from recorder.analysis_database import AnalysisDatabase
from recorder.evidence_packet import EVIDENCE_PACKET_VERSION, build_overlay_evidence_packet, evidence_packet_summary
from recorder.replay_scoring import standing_regime_family_gate_tuning_summary
from recorder.standing_regimes import (
    BlindRegimeAIReviewBatch,
    BlindRegimeCandidateReview,
    OpenAIBlindRegimeReviewer,
    RegimePolicyDraft,
    STANDING_REGIME_VERSION,
    active_standing_regimes_for_event,
    attach_blind_regime_promotion_waitlist,
    build_and_store_standing_regimes,
    build_standing_regime_outcome_report,
    classify_signal_for_regimes,
    discover_blind_regime_candidates,
    standing_regime_rows,
)


def _signal(**kwargs):
    defaults = {
        "source": "benzinga",
        "external_id": "id-1",
        "title": "Nvidia data center AI demand accelerates as hyperscaler capex rises",
        "url": "",
        "published_at": "2026-01-15T14:00:00+00:00",
        "region": "global",
        "theme": "market_news",
        "sentiment_score": 0.45,
        "sentiment_label": "positive",
        "confidence": 0.82,
        "assets": ("NVDA", "QQQ", "SOXX"),
        "asset_map_reason": "test",
        "market_reaction_status": "pending",
        "market_reaction": {},
        "prediction_gate_status": "eligible",
        "prediction_gate_reason": "test",
        "raw": {},
        "ingestion_version": "test-signals-v1",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def test_classifies_ai_boom_as_risk_on_regime() -> None:
    matches = classify_signal_for_regimes(
        {
            "title": "AI data center GPU demand lifts semiconductor capex",
            "region": "global",
            "theme": "market_news",
            "assets_json": '["NVDA","QQQ"]',
            "confidence": 0.8,
            "source": "marketaux",
        }
    )

    assert any(item["family"] == "ai_infrastructure_boom" for item in matches)
    assert {item["directional_bias"] for item in matches if item["family"] == "ai_infrastructure_boom"} == {"risk_on"}


def test_classifies_latin_america_oil_supply_risk() -> None:
    matches = classify_signal_for_regimes(
        {
            "title": "Venezuela wants oil firms to supply their own power as sanctions relief talks continue",
            "region": "global",
            "theme": "energy_supply",
            "assets_json": '["USO","XLE","CVX","VIX"]',
            "confidence": 0.78,
            "source": "benzinga",
        }
    )

    assert any(item["family"] == "latin_america_oil_supply_risk" for item in matches)
    assert {
        item["directional_bias"]
        for item in matches
        if item["family"] == "latin_america_oil_supply_risk"
    } == {"risk_off"}


def test_classifies_critical_minerals_geopolitics_without_generic_critical_false_positive() -> None:
    matches = classify_signal_for_regimes(
        {
            "title": "G7 countries discuss critical minerals price floor and strategic stockpiling agenda",
            "region": "global",
            "theme": "broad_geopolitical",
            "assets_json": '["REMX","PICK","QQQ","VIX"]',
            "confidence": 0.85,
            "source": "benzinga",
        }
    )

    assert any(item["family"] == "critical_minerals_geopolitics" for item in matches)
    assert {
        item["directional_bias"]
        for item in matches
        if item["family"] == "critical_minerals_geopolitics"
    } == {"risk_off"}

    false_positive_matches = classify_signal_for_regimes(
        {
            "title": "Analyst says critical earnings update may drive software shares",
            "region": "global",
            "theme": "market_news",
            "assets_json": '["QQQ","SPY"]',
            "confidence": 0.8,
            "source": "marketaux",
        }
    )

    assert not any(item["family"] == "critical_minerals_geopolitics" for item in false_positive_matches)


def test_builds_and_reports_standing_regime_episodes(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(_signal(external_id="ai-1"))
    db.upsert_external_market_signal(
        _signal(
            external_id="tariff-1",
            title="Trump tariff threat revives global trade war risk for semiconductors",
            published_at="2026-01-16T14:00:00+00:00",
            theme="trade_conflict",
            sentiment_score=-0.5,
            sentiment_label="negative",
            assets=("QQQ", "SMH", "SOXX", "VIX"),
        )
    )

    result = build_and_store_standing_regimes(db, start="2026-01-01", end="2026-01-31")
    rows = standing_regime_rows(db, cutoff_at="2026-01-20", limit=20)

    assert result["version"] == STANDING_REGIME_VERSION
    assert result["episodes"] >= 2
    assert any(row["family"] == "ai_infrastructure_boom" and row["directional_bias"] == "risk_on" for row in rows)
    assert any(row["family"] == "global_tariff_trade_war" and row["directional_bias"] == "risk_off" for row in rows)


def test_evidence_packet_includes_standing_regime_overlay(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(_signal(external_id="ai-packet-1"))
    build_and_store_standing_regimes(db, start="2026-01-01", end="2026-01-31")
    signals = db.recent_external_market_signals(limit=20)
    row = {
        "symbol": "NVDA",
        "actual_subject_symbol": "NVDA",
        "replay_timestamp": "2026-01-20T15:00:00+00:00",
        "prediction_cutoff_at": "2026-01-20T15:00:00+00:00",
        "predicted_direction": "up",
        "event_type": "earnings",
        "event_subtype": "guidance",
        "horizon": "1D",
        "session": "regular",
    }

    packet = build_overlay_evidence_packet(row, signals, db)

    assert packet["packet_version"] == EVIDENCE_PACKET_VERSION
    assert packet["standing_regime_overlay"]["bias"] == "risk_on"
    assert packet["standing_regime_overlay"]["matched_regime_count"] >= 1
    assert packet["standing_regime_overlay"]["confirmation_count"] >= 1
    assert packet["standing_regime_overlay"]["active_regimes"][0]["freshness_status"] in {"fresh_confirmation", "confirmed"}
    assert packet["standing_regime_overlay"]["active_regimes"][0]["relation_to_prediction"] == "confirm"
    assert any(vote["desk"] == "standing_regime_overlay" for vote in packet["tri_desk_alignment"]["desk_votes"])


def test_standing_regime_overlay_is_point_in_time_and_does_not_use_future_episode_titles(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(
        _signal(
            external_id="ai-before-cutoff",
            title="Nvidia data center AI demand accelerates before cutoff",
            published_at="2026-01-15T14:00:00+00:00",
        )
    )
    db.upsert_external_market_signal(
        _signal(
            external_id="ai-after-cutoff",
            title="Future AI data center order appears after cutoff and must not leak",
            published_at="2026-02-10T14:00:00+00:00",
        )
    )
    build_and_store_standing_regimes(db, start="2026-01-01", end="2026-02-28")

    overlay = active_standing_regimes_for_event(
        db,
        symbol="NVDA",
        cutoff_at="2026-01-20T15:00:00+00:00",
        predicted_direction="up",
    )

    assert overlay["point_in_time_rebuild"] is True
    assert overlay["bias"] == "risk_on"
    assert overlay["confirmation_count"] >= 1
    regime = overlay["active_regimes"][0]
    assert regime["evidence_count"] == 1
    assert regime["last_seen_at"].startswith("2026-01-15")
    assert all("Future AI" not in title for title in regime["latest_titles"])


def test_standing_regime_overlay_decays_aging_regimes_and_marks_prediction_conflict(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(
        _signal(
            external_id="tariff-aging",
            title="Trump tariff threat revives global trade war risk for semiconductors",
            published_at="2026-01-01T14:00:00+00:00",
            theme="trade_conflict",
            sentiment_score=-0.5,
            sentiment_label="negative",
            assets=("QQQ", "SMH", "SOXX", "VIX"),
        )
    )

    overlay = active_standing_regimes_for_event(
        db,
        symbol="QQQ",
        cutoff_at="2026-01-25T15:00:00+00:00",
        predicted_direction="up",
    )

    assert overlay["bias"] == "risk_off"
    assert overlay["conflict_count"] >= 1
    regime = overlay["active_regimes"][0]
    assert regime["status"] == "easing"
    assert regime["freshness_status"] == "aging"
    assert 0.0 < regime["status_decay"] < 1.0
    assert regime["effective_weight"] < regime["base_weight"]
    assert regime["relation_to_prediction"] == "conflict"


def test_evidence_packet_summary_groups_critical_minerals_family(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(
        _signal(
            external_id="critical-minerals-packet-1",
            title="G7 countries discuss critical minerals price floor and strategic stockpiling agenda",
            published_at="2026-01-15T14:00:00+00:00",
            region="global",
            theme="broad_geopolitical",
            assets=("REMX", "PICK", "NVDA", "QQQ", "VIX"),
            sentiment_score=-0.35,
            sentiment_label="negative",
            confidence=0.86,
        )
    )
    build_and_store_standing_regimes(db, start="2026-01-01", end="2026-01-31")
    signals = db.recent_external_market_signals(limit=20)
    row = {
        "symbol": "NVDA",
        "actual_subject_symbol": "NVDA",
        "replay_timestamp": "2026-01-20T15:00:00+00:00",
        "prediction_cutoff_at": "2026-01-20T15:00:00+00:00",
        "predicted_direction": "up",
        "actual_direction": "down",
        "direction_hit": 0,
        "actual_excess_return": -0.02,
        "event_type": "earnings",
        "event_subtype": "guidance",
        "horizon": "1D",
        "session": "regular",
    }

    packet = build_overlay_evidence_packet(row, signals, db)
    summary = evidence_packet_summary([row], signals, db)

    assert any(
        item["family"] == "critical_minerals_geopolitics"
        for item in packet["standing_regime_overlay"]["active_regimes"]
    )
    assert any(
        item["bucket"] == "critical_minerals_geopolitics"
        for item in summary["by_standing_regime_family"]
    )


def test_standing_regime_family_gate_tuning_finds_critical_minerals_conflict(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(
        _signal(
            external_id="critical-minerals-gate-1",
            title="G7 countries discuss critical minerals price floor and strategic stockpiling agenda",
            published_at="2026-01-15T14:00:00+00:00",
            region="global",
            theme="broad_geopolitical",
            assets=("REMX", "PICK", "NVDA", "QQQ", "VIX"),
            sentiment_score=-0.35,
            sentiment_label="negative",
            confidence=0.86,
        )
    )
    build_and_store_standing_regimes(db, start="2026-01-01", end="2026-01-31")
    signals = db.recent_external_market_signals(limit=20)
    rows = [
        {
            "symbol": "NVDA",
            "actual_subject_symbol": "NVDA",
            "replay_timestamp": "2026-01-20T15:00:00+00:00",
            "prediction_cutoff_at": "2026-01-20T15:00:00+00:00",
            "predicted_direction": "up",
            "actual_direction": "down",
            "direction_hit": 0,
            "actual_raw_return": -0.015,
            "actual_excess_return": -0.02,
            "confidence": 0.62,
            "event_type": "earnings",
            "event_subtype": "guidance",
            "horizon": "1D",
            "session": "regular",
            "raw_json": "{}",
        },
        {
            "symbol": "NVDA",
            "actual_subject_symbol": "NVDA",
            "replay_timestamp": "2026-01-21T15:00:00+00:00",
            "prediction_cutoff_at": "2026-01-21T15:00:00+00:00",
            "predicted_direction": "down",
            "actual_direction": "down",
            "direction_hit": 1,
            "actual_raw_return": -0.018,
            "actual_excess_return": -0.019,
            "confidence": 0.60,
            "event_type": "earnings",
            "event_subtype": "guidance",
            "horizon": "1D",
            "session": "regular",
            "raw_json": "{}",
        },
    ]

    result = standing_regime_family_gate_tuning_summary(
        rows,
        signals,
        db,
        conflict_score_grid=(0.0,),
        min_family_rows=1,
        min_directional_coverage=0.0,
        top_n=10,
    )

    assert any(item["family"] == "critical_minerals_geopolitics" for item in result["family_profiles"])
    assert any(item["family"] == "critical_minerals_geopolitics" for item in result["top_candidates"])


def test_blind_discovery_finds_unknown_long_running_candidate(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    for month in range(1, 5):
        for index in range(3):
            db.upsert_external_market_signal(
                _signal(
                    external_id=f"quantum-{month}-{index}",
                    title=f"Quantum computing backlog expands for cybersecurity workloads month {month} item {index}",
                    published_at=f"2026-{month:02d}-{10 + index:02d}T14:00:00+00:00",
                    region="global",
                    theme="market_news",
                    assets=("IBM", "MSFT", "QQQ"),
                    sentiment_score=0.35,
                    sentiment_label="positive",
                )
            )

    result = discover_blind_regime_candidates(
        db,
        start="2026-01-01",
        end="2026-04-30",
        min_evidence=8,
        min_duration_days=60,
        min_active_months=3,
        top_n=10,
    )

    assert result["returned_candidate_count"] >= 1
    assert any("quantum" in item["top_tokens"] for item in result["candidates"])


def test_openai_blind_reviewer_attaches_structured_ai_verdict() -> None:
    result = {
        "candidates": [
            {
                "candidate_key": "region_theme_token:global:market_news:quantum",
                "status": "active",
                "top_region": "global",
                "top_theme": "market_news",
                "evidence_count": 12,
                "duration_days": 100,
                "active_month_count": 4,
                "avg_sentiment": 0.4,
                "avg_confidence": 0.8,
                "top_tokens": ["quantum", "cybersecurity"],
                "top_assets": ["IBM", "MSFT", "QQQ"],
                "latest_titles": ["Quantum computing backlog expands for cybersecurity workloads"],
                "source_counts": {"benzinga": 12},
            }
        ]
    }
    parsed = BlindRegimeAIReviewBatch(
        summary="Quantum computing appears coherent enough to monitor.",
        reviews=[
            BlindRegimeCandidateReview(
                candidate_key="region_theme_token:global:market_news:quantum",
                verdict="monitor_only",
                proposed_family="quantum_cybersecurity_capex_theme",
                regime_name="Quantum/cybersecurity infrastructure theme",
                directional_bias="risk_on",
                confidence=0.72,
                affected_assets=["IBM", "MSFT", "QQQ"],
                rationale="Persistent but evidence is still too narrow for threshold promotion.",
                threshold_profile_hint="Monitor until more asset-specific reaction evidence appears.",
                missing_evidence=["market reaction validation"],
                profile_draft=RegimePolicyDraft(
                    archetype="technology_boom",
                    promotion_readiness="needs_review",
                    beneficiary_assets=["IBM", "MSFT"],
                    victim_assets=[],
                    hedge_assets=["QQQ"],
                    positive_news_meaning="regime_intensifying",
                    negative_news_meaning="regime_easing",
                    neutral_news_meaning="monitor_only",
                    intensifying_threshold_action="Raise positive regime context weight for validated quantum/cybersecurity beneficiaries.",
                    easing_threshold_action="Reduce theme boost if backlog or capex evidence fades.",
                    neutral_threshold_action="Keep as freshness-only context until market-reaction validation improves.",
                    resolution_prompt="Classify future headlines as intensifying, easing, resolved, false_resolution, or monitor_only; require sustained demand slowdown and market confirmation before resolution.",
                    resolution_confirmation_requirements=["follow-up company evidence", "market reaction validation"],
                    post_resolution_threshold_action="Remove theme boost only after repeated easing/resolution evidence.",
                    regime_status_hint="Treat as monitor-only until broader evidence confirms durable capex transmission.",
                ),
            )
        ],
    )

    class FakeResponses:
        def parse(self, **kwargs):
            assert kwargs["text_format"] is BlindRegimeAIReviewBatch
            return SimpleNamespace(output_parsed=parsed, id="resp_test")

    reviewer = OpenAIBlindRegimeReviewer(
        api_key="test",
        model="gpt-test",
        client=SimpleNamespace(responses=FakeResponses()),
    )
    reviewed = reviewer.review(result, max_candidates=1)

    assert reviewed["ai_review"]["verdict_counts"] == {"monitor_only": 1}
    assert reviewed["candidates"][0]["ai_review"]["proposed_family"] == "quantum_cybersecurity_capex_theme"
    assert reviewed["candidates"][0]["ai_review"]["profile_draft"]["promotion_readiness"] == "needs_review"
    assert reviewed["candidates"][0]["ai_review"]["profile_draft"]["archetype"] == "technology_boom"
    assert reviewed["candidates"][0]["ai_review"]["profile_draft"]["resolution_prompt"]
    assert reviewed["promotion_waitlist"]["watchlist_count"] == 1
    assert reviewed["promotion_waitlist"]["candidates"][0]["resolution_prompt"]


def test_blind_regime_promotion_waitlist_promotes_ready_review_with_resolution_prompt() -> None:
    result = {
        "candidates": [
            {
                "candidate_key": "region_theme_token:global:market_news:ai-power-grid",
                "status": "active",
                "evidence_count": 18,
                "duration_days": 120,
                "active_month_count": 4,
                "top_assets": ["NVDA", "VST", "QQQ"],
                "ai_review": {
                    "verdict": "promote_candidate",
                    "proposed_family": "ai_power_grid_constraint_theme",
                    "regime_name": "AI power-grid capacity constraint",
                    "directional_bias": "mixed",
                    "confidence": 0.78,
                    "affected_assets": ["NVDA", "VST", "QQQ"],
                    "threshold_profile_hint": "Treat fresh power-grid constraint headlines as a thematic threshold modifier.",
                    "profile_draft": {
                        "archetype": "technology_boom",
                        "promotion_readiness": "ready",
                        "beneficiary_assets": ["VST"],
                        "victim_assets": ["NVDA", "QQQ"],
                        "hedge_assets": ["QQQ"],
                        "resolution_prompt": "Classify updates into intensifying/easing/resolved/false_resolution; require capex, grid capacity, and market confirmation before resolution.",
                        "resolution_confirmation_requirements": ["capacity additions confirmed", "beneficiary/victim spread normalizes"],
                        "post_resolution_threshold_action": "Return theme modifier to neutral after repeated resolution evidence.",
                    },
                },
            }
        ]
    }

    reviewed = attach_blind_regime_promotion_waitlist(result)

    assert reviewed["promotion_waitlist"]["waitlist_count"] == 1
    candidate = reviewed["promotion_waitlist"]["candidates"][0]
    assert candidate["status"] == "promotion_waitlist"
    assert candidate["safety"]["live_threshold_promoted"] is False


def test_standing_regime_outcome_report_records_and_evaluates_learning_log(tmp_path) -> None:
    db = AnalysisDatabase(tmp_path / "market_events.db")
    db.upsert_external_market_signal(
        _signal(
            external_id="ai-outcome-1",
            title="Nvidia data center AI demand accelerates as hyperscaler capex rises",
            published_at="2026-01-20T14:55:00+00:00",
            assets=("NVDA", "QQQ", "SOXX"),
        )
    )
    with db.connect() as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_baselines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                captured_at TEXT,
                selected_price REAL,
                selected_price_source TEXT
            );
            CREATE TABLE IF NOT EXISTS market_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                captured_at TEXT,
                reaction_price REAL,
                price_source TEXT
            );
            """
        )
        conn.execute(
            "INSERT INTO market_baselines(news_id,symbol,captured_at,selected_price,selected_price_source,currency) VALUES (?,?,?,?,?,?)",
            (1, "QQQ", "2026-01-20T15:00:00+00:00", 100.0, "fixture", "USD"),
        )
        conn.execute(
            """
            INSERT INTO market_reactions(
                target_id,news_id,symbol,horizon,due_at,captured_at,baseline_price,
                reaction_price,raw_return,price_source,created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                1,
                1,
                "QQQ",
                "5M",
                "2026-01-20T15:05:00+00:00",
                "2026-01-20T15:05:00+00:00",
                100.0,
                101.0,
                0.01,
                "fixture",
                "2026-01-20T15:05:00+00:00",
            ),
        )
    recorded = build_standing_regime_outcome_report(
        db,
        symbols=("QQQ",),
        horizons_minutes="5",
        evaluate_pending=False,
        now="2026-01-20T15:00:00+00:00",
    )
    result = build_standing_regime_outcome_report(
        db,
        symbols=("QQQ",),
        horizons_minutes="5",
        record_current_snapshot=False,
        now="2026-01-20T15:05:00+00:00",
    )

    assert recorded["recorded_count"] == 1
    assert result["evaluated_count"] == 1
    assert result["latest_rows"][0]["family"] == "ai_infrastructure_boom"
    assert result["latest_rows"][0]["direction_hit"] == 1
    assert result["latest_rows"][0]["signed_return"] > 0
    assert result["safety"]["broker_orders_submitted"] is False
