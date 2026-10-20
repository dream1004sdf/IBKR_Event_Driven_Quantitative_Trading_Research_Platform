from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from recorder.live_learning_control_plane import (
    LiveLearningControlPlaneConfig,
    build_live_learning_control_plane,
    compact_m232_hot_cache_for_scorer,
    live_learning_control_plane_markdown,
    write_live_learning_hot_caches,
    _build_live_learning_hot_cache,
    _m232_adaptive_schedule,
    _m232_source_state,
)


def _write_json(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_live_learning_control_plane_builds_four_learning_sections(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE external_market_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT 'unknown',
            theme TEXT NOT NULL DEFAULT 'unknown',
            sentiment_score REAL,
            sentiment_label TEXT NOT NULL DEFAULT 'unknown',
            confidence REAL NOT NULL DEFAULT 0,
            assets_json TEXT NOT NULL DEFAULT '[]',
            asset_map_reason TEXT NOT NULL DEFAULT '',
            market_reaction_status TEXT NOT NULL DEFAULT 'pending',
            market_reaction_json TEXT NOT NULL DEFAULT '{}',
            prediction_gate_status TEXT NOT NULL DEFAULT 'blocked',
            prediction_gate_reason TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE option_structure_outcome_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            source_quote_generated_at TEXT NOT NULL DEFAULT '',
            structure_generated_at TEXT NOT NULL DEFAULT '',
            horizon TEXT NOT NULL,
            baseline_at TEXT,
            reaction_at TEXT,
            baseline_price REAL,
            reaction_price REAL,
            raw_return REAL,
            direction_label TEXT NOT NULL DEFAULT 'unknown',
            price_source TEXT NOT NULL,
            price_status TEXT NOT NULL DEFAULT 'pending',
            blocker_reason TEXT NOT NULL DEFAULT '',
            gamma_regime TEXT NOT NULL DEFAULT '',
            total_gamma_exposure REAL,
            gamma_flip REAL,
            call_wall REAL,
            put_wall REAL,
            max_pain REAL,
            distance_to_pin_pct REAL,
            pin_risk INTEGER NOT NULL DEFAULT 0,
            proximity_alerts_json TEXT NOT NULL DEFAULT '[]',
            expiry_bucket_summary_json TEXT NOT NULL DEFAULT '[]',
            confidence TEXT NOT NULL DEFAULT '',
            confidence_score REAL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            structure_json TEXT NOT NULL DEFAULT '{}',
            outcome_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE m223_live_learning_predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            predicted_direction TEXT NOT NULL,
            expected_return REAL NOT NULL,
            confidence REAL NOT NULL,
            packet_quality_level TEXT NOT NULL DEFAULT '',
            timestamp_alignment_status TEXT NOT NULL DEFAULT '',
            baseline_is_late INTEGER NOT NULL DEFAULT 0,
            option_structure_context_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE m223_live_learning_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER NOT NULL,
            horizon_minutes INTEGER NOT NULL,
            raw_return REAL,
            actual_direction TEXT NOT NULL DEFAULT 'unknown',
            direction_hit INTEGER,
            absolute_error REAL,
            status TEXT NOT NULL DEFAULT 'pending',
            evaluated_at TEXT
        );
        CREATE TABLE standing_market_regimes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime_key TEXT NOT NULL,
            family TEXT NOT NULL,
            regime_name TEXT NOT NULL,
            directional_bias TEXT NOT NULL,
            status TEXT NOT NULL,
            region TEXT NOT NULL,
            theme TEXT NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT NOT NULL DEFAULT '',
            severity_score REAL NOT NULL,
            confidence REAL NOT NULL,
            affected_assets_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            latest_titles_json TEXT NOT NULL DEFAULT '[]',
            threshold_profile_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            ingestion_version TEXT NOT NULL DEFAULT 'test',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    now_iso = datetime.now(UTC).isoformat()
    for idx in range(5):
        con.execute(
            """
            INSERT INTO external_market_signals(
                source,external_id,title,published_at,region,theme,sentiment_score,sentiment_label,confidence,
                assets_json,market_reaction_status,market_reaction_json,prediction_gate_status,raw_json,ingestion_version,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "emergency_breaking_news",
                f"e{idx}",
                "Breaking missile strike raises oil shock risk",
                now_iso,
                "middle_east",
                "energy",
                -0.7,
                "negative",
                0.8,
                json.dumps([{"symbol": "SPY"}, {"symbol": "VIX"}]),
                "complete",
                json.dumps({"raw_return": -0.003, "direction": "down"}),
                "watch",
                json.dumps({"urgency": {"urgency": "critical"}}),
                "test",
                now_iso,
                now_iso,
            ),
        )
    for idx in range(4):
        con.execute(
            """
            INSERT INTO option_structure_outcome_logs(
                snapshot_id,symbol,horizon,raw_return,direction_label,price_source,price_status,gamma_regime,
                pin_risk,proximity_alerts_json,expiry_bucket_summary_json,confidence,outcome_version,created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                f"s{idx}",
                "QQQ",
                "15M",
                0.002,
                "up",
                "fake",
                "complete",
                "negative_gamma_trend_amplification_bias",
                0,
                json.dumps(["near_call_wall"]),
                json.dumps([{"bucket": "0DTE", "gamma_regime": "negative_gamma_trend_amplification_bias"}]),
                "medium",
                "test",
                "2026-07-01T14:00:00+00:00",
                "2026-07-01T14:00:00+00:00",
            ),
        )
    for idx in range(8):
        cur = con.execute(
            """
            INSERT INTO m223_live_learning_predictions(
                symbol,predicted_direction,expected_return,confidence,packet_quality_level,
                timestamp_alignment_status,baseline_is_late,option_structure_context_json
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                "SPY",
                "down",
                -0.002,
                0.7,
                "usable",
                "aligned",
                0,
                json.dumps({"freshness_status": "fresh"}),
            ),
        )
        con.execute(
            """
            INSERT INTO m223_live_learning_outcomes(
                prediction_id,horizon_minutes,raw_return,actual_direction,direction_hit,absolute_error,status,evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (cur.lastrowid, 15, -0.003, "down", 1, 0.001, "evaluated", "2026-07-01T14:15:00+00:00"),
        )
    con.execute(
        """
        INSERT INTO standing_market_regimes(
            regime_key,family,regime_name,directional_bias,status,region,theme,
            started_at,last_seen_at,severity_score,confidence,affected_assets_json,
            evidence_count,latest_titles_json,threshold_profile_json,raw_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "global_tariff_trade_war:global:trade_conflict",
            "global_tariff_trade_war",
            "Global tariff pressure",
            "risk_off",
            "active",
            "global",
            "trade_conflict",
            "2026-06-01T14:00:00+00:00",
            "2026-07-01T14:00:00+00:00",
            2.5,
            0.8,
            json.dumps(["SPY", "QQQ", "VIX"]),
            18,
            json.dumps(["Tariff escalation hits risk assets"]),
            json.dumps({"negative_regime_news": "tariff escalation confirms risk-off regime"}),
            "{}",
            "2026-07-01T14:00:00+00:00",
            "2026-07-01T14:00:00+00:00",
        ),
    )
    con.commit()
    con.close()

    m223_path = _write_json(
        tmp_path / "m223.json",
        {"latest_status": "collecting", "latest_counts": {"prediction_count": 8, "evaluated_outcome_count": 8, "adjustment_note_count": 1}},
    )
    m226_path = _write_json(
        tmp_path / "m226.json",
        {
            "promotion_ready_count": 1,
            "promotion_gate": {
                "waitlist_count": 1,
                "watchlist_count": 0,
                "blocked_count": 0,
                "waitlist_candidates": [{"semantic_family_key": "x"}],
                "scorer_evidence_packet_hooks": [{"application_policy": "conditional_only"}],
                "scorer_threshold_application_policies": [{"scorer_action": "confidence_multiplier"}],
            },
        },
    )
    m250_path = _write_json(
        tmp_path / "m250.json",
        {
            "limited_auto_trading_readiness_version": "m250-test",
            "generated_at": "2026-07-01T14:30:00+00:00",
            "summary": {
                "gate_execution_posture": "size_reduced",
                "gate_size_multiplier": 0.5,
                "gate_confidence_multiplier": 0.85,
            },
            "contexts": {
                "gate_severity_policy": {
                    "recommended_execution_posture": "size_reduced",
                    "size_multiplier": 0.5,
                    "confidence_multiplier": 0.85,
                    "category_counts": {"haircut": 1},
                    "signals": [{"code": "market_temperature_elevated", "category": "haircut"}],
                }
            },
        },
    )

    result = build_live_learning_control_plane(
        LiveLearningControlPlaneConfig(
            database_path=db_path,
            live_scorer_learning_json_path=m223_path,
            threshold_candidate_evaluator_json_path=m226_path,
            limited_auto_readiness_json_path=m250_path,
        )
    )

    assert result["live_scorer_application"]["paper_scorer_ready"] is True
    assert result["live_scorer_application"]["gate_severity_overlay"]["posture"] == "size_reduced"
    assert result["live_scorer_application"]["scorer_threshold_application_policies"][-1]["source"] == "m250_gate_severity_policy"
    assert result["live_scorer_application"]["scorer_threshold_application_policies"][-1]["confidence_multiplier"] == 0.85
    assert result["emergency_news_reflex_memory"]["reflex_candidate_count"] >= 1
    assert result["options_structure_learning"]["memory_count"] >= 1
    assert result["standing_regime_overlay_learning"]["active_or_easing_count"] == 1
    assert result["standing_regime_overlay_learning"]["bias_counts"]["risk_off"] == 1
    assert result["geopolitical_overlay_learning"]["available"] is True
    assert result["geopolitical_overlay_learning"]["hot_bucket_count"] >= 1
    assert result["live_paper_action_gate_learning"]["paper_entry_candidate_count"] >= 1
    assert result["safety"]["broker_orders_submitted"] is False
    assert "Emergency News Reflex Memory" in live_learning_control_plane_markdown(result)
    assert "Geopolitical Overlay Hot Cache" in live_learning_control_plane_markdown(result)
    assert "Standing Regime Overlay Learning" in live_learning_control_plane_markdown(result)


def test_live_learning_control_plane_writes_lightweight_hot_caches(tmp_path: Path) -> None:
    db_path = tmp_path / "events.db"
    con = sqlite3.connect(db_path)
    con.executescript(
        """
        CREATE TABLE external_market_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL DEFAULT '',
            published_at TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT 'unknown',
            theme TEXT NOT NULL DEFAULT 'unknown',
            sentiment_score REAL,
            sentiment_label TEXT NOT NULL DEFAULT 'unknown',
            confidence REAL NOT NULL DEFAULT 0,
            assets_json TEXT NOT NULL DEFAULT '[]',
            asset_map_reason TEXT NOT NULL DEFAULT '',
            market_reaction_status TEXT NOT NULL DEFAULT 'pending',
            market_reaction_json TEXT NOT NULL DEFAULT '{}',
            prediction_gate_status TEXT NOT NULL DEFAULT 'blocked',
            prediction_gate_reason TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE standing_market_regimes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime_key TEXT NOT NULL,
            family TEXT NOT NULL,
            regime_name TEXT NOT NULL,
            directional_bias TEXT NOT NULL,
            status TEXT NOT NULL,
            region TEXT NOT NULL,
            theme TEXT NOT NULL,
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT NOT NULL DEFAULT '',
            severity_score REAL NOT NULL,
            confidence REAL NOT NULL,
            affected_assets_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            latest_titles_json TEXT NOT NULL DEFAULT '[]',
            threshold_profile_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            ingestion_version TEXT NOT NULL DEFAULT 'test',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    now_iso = datetime.now(UTC).isoformat()
    con.execute(
        """
        INSERT INTO external_market_signals(
            source,external_id,title,published_at,region,theme,sentiment_score,sentiment_label,confidence,
            assets_json,market_reaction_status,market_reaction_json,prediction_gate_status,raw_json,ingestion_version,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "emergency_breaking_news",
            "e1",
            "Breaking tariff escalation hits risk assets",
            now_iso,
            "global",
            "trade_conflict",
            -0.7,
            "negative",
            0.8,
            json.dumps(["SPY", "QQQ"]),
            "complete",
            json.dumps({"direction": "down"}),
            "watch",
            json.dumps({"urgency": {"urgency": "high"}}),
            "test",
            now_iso,
            now_iso,
        ),
    )
    con.execute(
        """
        INSERT INTO standing_market_regimes(
            regime_key,family,regime_name,directional_bias,status,region,theme,
            started_at,last_seen_at,severity_score,confidence,affected_assets_json,
            evidence_count,latest_titles_json,threshold_profile_json,raw_json,created_at,updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            "ai_infrastructure_boom:global:market_news",
            "ai_infrastructure_boom",
            "AI infrastructure boom",
            "risk_on",
            "active",
            "global",
            "market_news",
            "2026-06-01T14:00:00+00:00",
            "2026-07-01T14:00:00+00:00",
            2.2,
            0.82,
            json.dumps(["QQQ", "SMH", "NVDA"]),
            22,
            json.dumps(["AI capex continues to lift semis"]),
            json.dumps({"positive_regime_news": "fresh AI demand confirms favorable regime"}),
            "{}",
            "2026-07-01T14:00:00+00:00",
            "2026-07-01T14:00:00+00:00",
        ),
    )
    con.commit()
    state = _m232_source_state(con)
    con.close()
    m226_path = _write_json(tmp_path / "m226.json", {"promotion_gate": {"watchlist_count": 0, "blocked_count": 0}})
    m250_path = _write_json(
        tmp_path / "m250.json",
        {
            "summary": {"gate_execution_posture": "size_reduced", "gate_size_multiplier": 0.5, "gate_confidence_multiplier": 0.85},
            "contexts": {"gate_severity_policy": {"size_multiplier": 0.5, "confidence_multiplier": 0.85}},
        },
    )
    config = LiveLearningControlPlaneConfig(
        database_path=db_path,
        threshold_candidate_evaluator_json_path=m226_path,
        limited_auto_readiness_json_path=m250_path,
        hot_cache_json_path=tmp_path / "hot.json",
        reflex_hot_cache_json_path=tmp_path / "reflex.json",
        scorer_overlay_cache_json_path=tmp_path / "scorer.json",
        option_structure_cache_json_path=tmp_path / "option.json",
        action_gate_cache_json_path=tmp_path / "action.json",
        hot_limit=50,
    )
    schedule = _m232_adaptive_schedule(
        config=config,
        previous_state=None,
        current_state=state,
        seconds_until_full_due=120.0,
    )
    cache = _build_live_learning_hot_cache(config, source_state=state, schedule=schedule)
    written = write_live_learning_hot_caches(config, cache)

    assert schedule["tier"] == "hot_emergency"
    assert cache["max_consumer_age_seconds"] >= 120.0
    assert cache["safety"]["hot_path_cache_only"] is True
    assert cache["standing_regime_cache"]["active_or_easing_count"] == 1
    assert cache["geopolitical_hot_cache"]["available"] is True
    assert cache["scorer_overlay_cache"]["standing_regime_cache"]["bias_counts"]["risk_on"] == 1
    assert set(written) == {"hot_cache", "reflex_hot_cache", "scorer_overlay_cache", "option_structure_cache", "action_gate_cache"}
    assert json.loads((tmp_path / "hot.json").read_text(encoding="utf-8"))["m232_hot_cache_version"] == "m232-hot-cache-v1"
    assert json.loads((tmp_path / "scorer.json").read_text(encoding="utf-8"))["gate_severity_overlay"]["size_multiplier"] == 0.5
    assert json.loads((tmp_path / "action.json").read_text(encoding="utf-8"))["gate_severity_overlay"]["confidence_multiplier"] == 0.85
    scorer_context = compact_m232_hot_cache_for_scorer(tmp_path / "hot.json")
    assert scorer_context["usable"] is True
    assert scorer_context["adaptive_tier"] == "hot_emergency"
    assert scorer_context["reflex_candidate_count"] >= 0
    assert "reflex_top_assets" in scorer_context
    assert scorer_context["standing_regime_count"] == 1
    assert scorer_context["standing_regime_top"][0]["family"] == "ai_infrastructure_boom"
    assert "geopolitical_top_buckets" in scorer_context
