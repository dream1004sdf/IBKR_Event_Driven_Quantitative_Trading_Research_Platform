from __future__ import annotations

import json
import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .analysis_models import NewsAnalysis
from .attribution import ATTRIBUTION_ENGINE_VERSION, AttributionDecision, attribute_event_targets
from .calendar_utils import evaluation_label
from .database import Database, parse_utc, utc_iso
from .event_clusters import build_cluster_identity, cluster_member_role, cluster_similarity, source_rank
from .news_lineage_v2 import (
    assign_article_to_event_cluster,
    ensure_news_lineage_v2_schema,
    link_external_signal_to_source_news,
    recompute_event_cluster_roles,
    upsert_raw_news_processing_status,
)
from .prediction_quality import evaluate_prediction_quality
from .pipeline_diagnostics import (
    DIAGNOSTIC_POLICY_VERSION,
    GATE_AUDIT_SCHEMA,
    derive_gate_decisions,
    write_gate_audit,
)

UTC = timezone.utc

ANALYSIS_SCHEMA = """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_key TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS analysis_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER NOT NULL,
            analysis_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at TEXT NOT NULL,
            locked_by TEXT,
            locked_at TEXT,
            last_error TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES news_articles(id),
            UNIQUE(news_id, analysis_key),
            CHECK(status IN ('queued','processing','retry','complete','skipped','dead'))
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_jobs_ready
            ON analysis_jobs(analysis_key, status, next_attempt_at, priority, id);
        CREATE INDEX IF NOT EXISTS idx_analysis_jobs_lock
            ON analysis_jobs(status, locked_at);

        CREATE TABLE IF NOT EXISTS analysis_job_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            message TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES analysis_jobs(id)
        );

        CREATE INDEX IF NOT EXISTS idx_analysis_events_job
            ON analysis_job_events(job_id, id);

        CREATE TABLE IF NOT EXISTS news_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL UNIQUE,
            news_id INTEGER NOT NULL,
            analysis_key TEXT NOT NULL,
            provider TEXT NOT NULL,
            model_name TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            prompt_hash TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            response_id TEXT,
            started_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            duration_ms INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT NOT NULL,
            sentiment TEXT NOT NULL,
            causal_signal INTEGER NOT NULL,
            prediction_eligible INTEGER NOT NULL,
            analysis_priority TEXT NOT NULL,
            primary_symbol TEXT NOT NULL,
            related_symbols_json TEXT NOT NULL,
            impact_score REAL NOT NULL,
            novelty_score REAL NOT NULL,
            source_quality REAL NOT NULL,
            confidence REAL NOT NULL,
            time_horizon TEXT NOT NULL,
            summary TEXT NOT NULL,
            bull_case_json TEXT NOT NULL,
            bear_case_json TEXT NOT NULL,
            data_limitations_json TEXT NOT NULL,
            raw_json TEXT NOT NULL,
            input_tokens INTEGER,
            output_tokens INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES analysis_jobs(id),
            FOREIGN KEY(news_id) REFERENCES news_articles(id),
            UNIQUE(news_id, analysis_key)
        );

        CREATE INDEX IF NOT EXISTS idx_news_analyses_news
            ON news_analyses(news_id, completed_at);
        CREATE INDEX IF NOT EXISTS idx_news_analyses_type
            ON news_analyses(event_type, completed_at);

        CREATE TABLE IF NOT EXISTS prediction_inputs (
            analysis_id INTEGER PRIMARY KEY,
            news_id INTEGER NOT NULL,
            input_hash TEXT NOT NULL,
            headline_hash TEXT NOT NULL,
            body_hash TEXT NOT NULL,
            input_snapshot_json TEXT NOT NULL,
            baseline_snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(analysis_id) REFERENCES news_analyses(id),
            FOREIGN KEY(news_id) REFERENCES news_articles(id)
        );

        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            analysis_id INTEGER NOT NULL,
            news_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            horizon TEXT NOT NULL,
            expected_return REAL NOT NULL,
            bear_return REAL NOT NULL,
            bull_return REAL NOT NULL,
            up_probability REAL NOT NULL,
            flat_probability REAL NOT NULL,
            down_probability REAL NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            evaluation_due_at TEXT,
            is_valid INTEGER NOT NULL,
            invalid_reason TEXT,
            FOREIGN KEY(analysis_id) REFERENCES news_analyses(id),
            FOREIGN KEY(news_id) REFERENCES news_articles(id),
            UNIQUE(analysis_id, symbol, horizon)
        );

        CREATE INDEX IF NOT EXISTS idx_predictions_eval
            ON predictions(news_id, symbol, horizon, is_valid);

        CREATE TABLE IF NOT EXISTS official_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_id INTEGER,
            event_cluster_id INTEGER,
            source_type TEXT NOT NULL,
            source_url TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            content TEXT,
            content_hash TEXT,
            document_type TEXT NOT NULL DEFAULT '',
            event_type TEXT NOT NULL DEFAULT '',
            actual_subject_symbol TEXT NOT NULL DEFAULT '',
            published_at TEXT,
            accepted_at TEXT,
            first_seen_at TEXT,
            available_at TEXT,
            external_document_id TEXT NOT NULL DEFAULT '',
            accession_number TEXT NOT NULL DEFAULT '',
            form_type TEXT NOT NULL DEFAULT '',
            cik TEXT NOT NULL DEFAULT '',
            company_name TEXT NOT NULL DEFAULT '',
            ticker TEXT NOT NULL DEFAULT '',
            filing_date TEXT NOT NULL DEFAULT '',
            primary_document TEXT NOT NULL DEFAULT '',
            raw_snapshot TEXT,
            materiality_classification TEXT NOT NULL DEFAULT 'unclassified',
            materiality_reason TEXT NOT NULL DEFAULT '',
            is_amended INTEGER NOT NULL DEFAULT 0,
            supersedes_document_id INTEGER,
            ingestion_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate',
            last_error TEXT,
            discovered_at TEXT NOT NULL,
            fetched_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(news_id) REFERENCES news_articles(id),
            UNIQUE(source_url, news_id)
        );
        CREATE INDEX IF NOT EXISTS idx_official_documents_news
            ON official_documents(news_id,status);

        CREATE TABLE IF NOT EXISTS event_clusters (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_key TEXT NOT NULL UNIQUE,
            event_scope TEXT NOT NULL,
            event_type TEXT NOT NULL,
            cluster_family TEXT NOT NULL DEFAULT 'unknown',
            subject_key TEXT NOT NULL,
            primary_symbol TEXT NOT NULL,
            event_anchor TEXT NOT NULL,
            event_time TEXT NOT NULL,
            comparison_text TEXT NOT NULL DEFAULT '',
            representative_analysis_id INTEGER,
            representative_news_id INTEGER,
            representative_rank INTEGER NOT NULL DEFAULT 0,
            source_tier TEXT NOT NULL DEFAULT 'unknown',
            member_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(representative_analysis_id) REFERENCES news_analyses(id),
            FOREIGN KEY(representative_news_id) REFERENCES news_articles(id)
        );
        CREATE INDEX IF NOT EXISTS idx_event_clusters_lookup
            ON event_clusters(event_scope,event_type,subject_key,event_time);

        CREATE TABLE IF NOT EXISTS event_cluster_members (
            cluster_id INTEGER NOT NULL,
            analysis_id INTEGER NOT NULL UNIQUE,
            news_id INTEGER NOT NULL,
            member_role TEXT NOT NULL,
            similarity REAL NOT NULL,
            source_rank INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(cluster_id,analysis_id),
            FOREIGN KEY(cluster_id) REFERENCES event_clusters(id),
            FOREIGN KEY(analysis_id) REFERENCES news_analyses(id),
            FOREIGN KEY(news_id) REFERENCES news_articles(id)
        );

        CREATE TABLE IF NOT EXISTS historical_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            event_subtype TEXT NOT NULL DEFAULT '',
            actual_subject_symbol TEXT NOT NULL,
            official_document_id INTEGER NOT NULL,
            event_timestamp TEXT NOT NULL,
            available_at TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            prediction_cutoff TEXT NOT NULL,
            market_session TEXT NOT NULL DEFAULT 'unknown',
            replay_eligibility TEXT NOT NULL DEFAULT 'review_required',
            replay_eligibility_reasons_json TEXT NOT NULL DEFAULT '[]',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(official_document_id) REFERENCES official_documents(id),
            UNIQUE(event_type,actual_subject_symbol,official_document_id)
        );
        CREATE INDEX IF NOT EXISTS idx_historical_events_lookup
            ON historical_events(event_type,actual_subject_symbol,available_at);

        CREATE TABLE IF NOT EXISTS replay_corpus (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            corpus_uuid TEXT NOT NULL UNIQUE,
            historical_event_id INTEGER NOT NULL,
            official_document_id INTEGER NOT NULL,
            actual_subject_symbol TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT NOT NULL DEFAULT '',
            replay_timestamp TEXT NOT NULL,
            prediction_cutoff_at TEXT NOT NULL,
            session TEXT NOT NULL DEFAULT 'unknown',
            input_snapshot_hash TEXT NOT NULL,
            input_snapshot_json TEXT NOT NULL,
            timestamp_quality TEXT NOT NULL DEFAULT 'source_accepted',
            source_quality TEXT NOT NULL DEFAULT 'official_primary',
            leakage_status TEXT NOT NULL DEFAULT 'pending',
            leakage_reasons_json TEXT NOT NULL DEFAULT '[]',
            eligibility_status TEXT NOT NULL DEFAULT 'review_required',
            blocker_reason TEXT NOT NULL DEFAULT '',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(historical_event_id) REFERENCES historical_events(id),
            FOREIGN KEY(official_document_id) REFERENCES official_documents(id),
            UNIQUE(historical_event_id,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_corpus_lookup
            ON replay_corpus(event_type,actual_subject_symbol,replay_timestamp);

        CREATE TABLE IF NOT EXISTS replay_market_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            replay_corpus_id INTEGER NOT NULL,
            historical_event_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            benchmark_symbol TEXT NOT NULL DEFAULT '',
            horizon TEXT NOT NULL,
            baseline_at TEXT,
            reaction_at TEXT,
            baseline_price REAL,
            reaction_price REAL,
            raw_return REAL,
            benchmark_baseline_price REAL,
            benchmark_reaction_price REAL,
            benchmark_return REAL,
            excess_return REAL,
            direction_label TEXT NOT NULL DEFAULT 'unknown',
            price_source TEXT NOT NULL,
            price_status TEXT NOT NULL DEFAULT 'pending',
            blocker_reason TEXT NOT NULL DEFAULT '',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(replay_corpus_id) REFERENCES replay_corpus(id),
            FOREIGN KEY(historical_event_id) REFERENCES historical_events(id),
            UNIQUE(replay_corpus_id,horizon,price_source,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_market_outcomes_lookup
            ON replay_market_outcomes(symbol,horizon,reaction_at);

        CREATE TABLE IF NOT EXISTS replay_model_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            replay_corpus_id INTEGER NOT NULL,
            outcome_id INTEGER NOT NULL,
            historical_event_id INTEGER NOT NULL,
            official_document_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT NOT NULL DEFAULT '',
            replay_timestamp TEXT NOT NULL,
            prediction_cutoff_at TEXT NOT NULL,
            horizon TEXT NOT NULL,
            price_source TEXT NOT NULL,
            scorer_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            scorer_version TEXT NOT NULL,
            input_snapshot_hash TEXT NOT NULL,
            prediction_status TEXT NOT NULL DEFAULT 'complete',
            predicted_direction TEXT NOT NULL,
            expected_return REAL NOT NULL,
            bear_return REAL NOT NULL,
            bull_return REAL NOT NULL,
            up_probability REAL NOT NULL,
            flat_probability REAL NOT NULL,
            down_probability REAL NOT NULL,
            confidence REAL NOT NULL,
            rationale TEXT NOT NULL DEFAULT '',
            raw_json TEXT NOT NULL DEFAULT '{}',
            actual_direction TEXT NOT NULL,
            actual_raw_return REAL,
            actual_excess_return REAL,
            direction_hit INTEGER,
            absolute_raw_error REAL,
            absolute_excess_error REAL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(replay_corpus_id) REFERENCES replay_corpus(id),
            FOREIGN KEY(outcome_id) REFERENCES replay_market_outcomes(id),
            UNIQUE(outcome_id,scorer_name,model_name,scorer_version)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_model_scores_lookup
            ON replay_model_scores(symbol,horizon,scorer_name,created_at);

        CREATE TABLE IF NOT EXISTS financial_statement_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            cik TEXT NOT NULL DEFAULT '',
            taxonomy TEXT NOT NULL,
            tag TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            unit TEXT NOT NULL DEFAULT '',
            fiscal_year INTEGER,
            fiscal_period TEXT NOT NULL DEFAULT '',
            form TEXT NOT NULL DEFAULT '',
            filed_at TEXT NOT NULL DEFAULT '',
            start_date TEXT,
            end_date TEXT NOT NULL DEFAULT '',
            frame TEXT NOT NULL DEFAULT '',
            value REAL NOT NULL,
            source_url TEXT NOT NULL DEFAULT '',
            accession_number TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL,
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(symbol,taxonomy,tag,unit,fiscal_year,fiscal_period,end_date,accession_number,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_financial_statement_facts_lookup
            ON financial_statement_facts(symbol,tag,end_date);

        CREATE TABLE IF NOT EXISTS fred_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            series_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            value REAL NOT NULL,
            realtime_start TEXT NOT NULL DEFAULT '',
            realtime_end TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL,
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(series_id,observation_date,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_fred_observations_lookup
            ON fred_observations(series_id,observation_date);

        CREATE TABLE IF NOT EXISTS economic_indicator_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            canonical_series_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            native_series_id TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT NOT NULL DEFAULT '',
            frequency TEXT NOT NULL DEFAULT '',
            region TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            provider_priority INTEGER NOT NULL DEFAULT 50,
            realtime_start TEXT NOT NULL DEFAULT '',
            realtime_end TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            source_hash TEXT NOT NULL,
            ingestion_version TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(canonical_series_id,provider,native_series_id,observation_date,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_economic_indicator_observations_lookup
            ON economic_indicator_observations(canonical_series_id,observation_date,provider_priority);

        CREATE TABLE IF NOT EXISTS technical_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            as_of_date TEXT NOT NULL,
            price_source TEXT NOT NULL,
            close REAL NOT NULL,
            return_1d REAL,
            return_5d REAL,
            return_20d REAL,
            sma_20 REAL,
            sma_50 REAL,
            ema_12 REAL,
            ema_26 REAL,
            macd REAL,
            macd_signal REAL,
            rsi_14 REAL,
            volatility_20 REAL,
            source_start_date TEXT NOT NULL DEFAULT '',
            source_end_date TEXT NOT NULL DEFAULT '',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(symbol,as_of_date,price_source,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_technical_indicators_lookup
            ON technical_indicators(symbol,as_of_date,price_source);

        CREATE TABLE IF NOT EXISTS external_market_signals (
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
            updated_at TEXT NOT NULL,
            UNIQUE(source,external_id,ingestion_version)
        );
        CREATE INDEX IF NOT EXISTS idx_external_market_signals_lookup
            ON external_market_signals(source,published_at,region,theme);

        CREATE TABLE IF NOT EXISTS standing_market_regimes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            regime_key TEXT NOT NULL,
            family TEXT NOT NULL,
            regime_name TEXT NOT NULL,
            directional_bias TEXT NOT NULL DEFAULT 'mixed',
            status TEXT NOT NULL DEFAULT 'active',
            region TEXT NOT NULL DEFAULT 'global',
            theme TEXT NOT NULL DEFAULT 'unknown',
            started_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            resolved_at TEXT NOT NULL DEFAULT '',
            severity_score REAL NOT NULL DEFAULT 0,
            confidence REAL NOT NULL DEFAULT 0,
            affected_assets_json TEXT NOT NULL DEFAULT '[]',
            evidence_count INTEGER NOT NULL DEFAULT 0,
            source_counts_json TEXT NOT NULL DEFAULT '{}',
            latest_titles_json TEXT NOT NULL DEFAULT '[]',
            threshold_profile_json TEXT NOT NULL DEFAULT '{}',
            raw_json TEXT NOT NULL DEFAULT '{}',
            ingestion_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(regime_key,ingestion_version),
            CHECK(directional_bias IN ('risk_off','risk_on','mixed','neutral')),
            CHECK(status IN ('active','easing','resolved'))
        );
        CREATE INDEX IF NOT EXISTS idx_standing_market_regimes_lookup
            ON standing_market_regimes(status,family,last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_standing_market_regimes_time
            ON standing_market_regimes(started_at,resolved_at,last_seen_at);

        CREATE TABLE IF NOT EXISTS replay_surprise_adjustment_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            score_id INTEGER NOT NULL,
            outcome_id INTEGER NOT NULL,
            replay_corpus_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_subtype TEXT NOT NULL DEFAULT '',
            replay_timestamp TEXT NOT NULL,
            horizon TEXT NOT NULL,
            price_source TEXT NOT NULL,
            scorer_name TEXT NOT NULL,
            model_name TEXT NOT NULL,
            scorer_version TEXT NOT NULL,
            predicted_direction TEXT NOT NULL,
            actual_direction TEXT NOT NULL,
            expected_return REAL NOT NULL,
            actual_raw_return REAL,
            actual_excess_return REAL,
            mismatch_type TEXT NOT NULL,
            reason_category TEXT NOT NULL,
            reason_summary TEXT NOT NULL,
            recommended_adjustment TEXT NOT NULL,
            feature_hints_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '{}',
            analysis_provider TEXT NOT NULL DEFAULT 'deterministic',
            analysis_model TEXT NOT NULL DEFAULT '',
            adjustment_version TEXT NOT NULL,
            raw_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY(score_id) REFERENCES replay_model_scores(id),
            FOREIGN KEY(outcome_id) REFERENCES replay_market_outcomes(id),
            UNIQUE(score_id,analysis_provider,analysis_model,adjustment_version)
        );
        CREATE INDEX IF NOT EXISTS idx_replay_surprise_adjustment_notes_lookup
            ON replay_surprise_adjustment_notes(symbol,horizon,scorer_version,created_at);

        CREATE TABLE IF NOT EXISTS option_structure_outcome_logs (
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
            updated_at TEXT NOT NULL,
            UNIQUE(snapshot_id,symbol,horizon,price_source,outcome_version)
        );
        CREATE INDEX IF NOT EXISTS idx_option_structure_outcome_logs_lookup
            ON option_structure_outcome_logs(symbol,horizon,reaction_at);
        """



@dataclass(frozen=True)
class ClaimedJob:
    job_id: int
    news_id: int
    attempts: int
    analysis_key: str
    news: dict[str, Any]


class AnalysisDatabase(Database):
    """Backward-compatible extension of the v0.1 SQLite database."""

    def __init__(self, path: str | Path):
        super().__init__(path)
        self.initialize_analysis_schema()
        self._apply_v011_data_migrations()
        self._apply_v014_attribution_migration()
        self._apply_v014_sec_edgar_migration()
        self._apply_v014_macro_sources_migration()
        self._apply_v014_historical_events_migration()
        self._apply_v014_replay_corpus_migration()
        self._apply_v014_replay_market_outcomes_migration()
        self._apply_v014_replay_model_scores_migration()
        self._apply_v014_structured_sources_migration()
        self._apply_v014_external_market_signals_migration()
        self._apply_v014_surprise_adjustment_notes_migration()
        self._apply_v014_economic_indicator_observations_migration()
        self._apply_v014_option_structure_outcome_logs_migration()
        self._apply_v014_unknown_theme_candidates_migration()

    def initialize_analysis_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(ANALYSIS_SCHEMA)
            conn.executescript(GATE_AUDIT_SCHEMA)
            ensure_news_lineage_v2_schema(conn)
            self._ensure_column(conn, "news_analyses", "event_scope", "TEXT NOT NULL DEFAULT 'company'")
            self._ensure_column(conn, "news_analyses", "subject_entity", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "reaction_observation_eligible", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "news_analyses", "source_tier", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "news_analyses", "official_source_status", "TEXT NOT NULL DEFAULT 'not_available'")
            self._ensure_column(conn, "news_analyses", "event_cluster_id", "INTEGER")
            self._ensure_column(conn, "news_analyses", "event_subject_symbol", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "subject_asset_type", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "news_analyses", "target_asset_type", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "news_analyses", "target_role", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "news_analyses", "target_assets_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "news_analyses", "attribution_status", "TEXT NOT NULL DEFAULT 'unresolved'")
            self._ensure_column(conn, "news_analyses", "attribution_confidence", "REAL NOT NULL DEFAULT 0")
            self._ensure_column(conn, "news_analyses", "original_subscription_symbol", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "subject_target_available", "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column(conn, "news_analyses", "attribution_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "article_scope", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "news_analyses", "article_primary_subject", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "mentioned_symbols_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "news_analyses", "subscription_entity_role", "TEXT NOT NULL DEFAULT 'delivery_route'")
            self._ensure_column(conn, "news_analyses", "target_quote_integrity_status", "TEXT NOT NULL DEFAULT 'not_available'")
            self._ensure_column(conn, "news_analyses", "target_quote_integrity_reasons_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "news_analyses", "delivery_reference_symbol", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "delivery_reference_price", "REAL")
            self._ensure_column(conn, "news_analyses", "delivery_reference_price_source", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "document_type", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "event_type", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "actual_subject_symbol", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "published_at", "TEXT")
            self._ensure_column(conn, "official_documents", "accepted_at", "TEXT")
            self._ensure_column(conn, "official_documents", "first_seen_at", "TEXT")
            self._ensure_column(conn, "official_documents", "available_at", "TEXT")
            self._ensure_column(conn, "official_documents", "external_document_id", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "accession_number", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "form_type", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "cik", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "company_name", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "ticker", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "filing_date", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "primary_document", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "raw_snapshot", "TEXT")
            self._ensure_column(conn, "official_documents", "materiality_classification", "TEXT NOT NULL DEFAULT 'unclassified'")
            self._ensure_column(conn, "official_documents", "materiality_reason", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "official_documents", "is_amended", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "official_documents", "supersedes_document_id", "INTEGER")
            self._ensure_column(conn, "official_documents", "ingestion_version", "TEXT NOT NULL DEFAULT ''")
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_official_documents_sec_accession
                   ON official_documents(source_type,accession_number)
                   WHERE source_type='sec' AND accession_number<>''"""
            )
            conn.execute(
                """CREATE INDEX IF NOT EXISTS idx_official_documents_sec_subject
                   ON official_documents(source_type,actual_subject_symbol,available_at)"""
            )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_official_documents_external_id
                   ON official_documents(source_type,external_document_id)
                   WHERE external_document_id<>''"""
            )
            self._ensure_column(conn, "historical_events", "event_subtype", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "historical_events", "market_session", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "historical_events", "replay_eligibility", "TEXT NOT NULL DEFAULT 'review_required'")
            self._ensure_column(conn, "historical_events", "replay_eligibility_reasons_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "replay_corpus", "event_subtype", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "replay_corpus", "input_snapshot_json", "TEXT NOT NULL DEFAULT '{}'")
            self._ensure_column(conn, "replay_corpus", "leakage_reasons_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "replay_market_outcomes", "benchmark_symbol", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "replay_market_outcomes", "benchmark_baseline_price", "REAL")
            self._ensure_column(conn, "replay_market_outcomes", "benchmark_reaction_price", "REAL")
            self._ensure_column(conn, "replay_market_outcomes", "benchmark_return", "REAL")
            self._ensure_column(conn, "replay_market_outcomes", "excess_return", "REAL")
            self._ensure_column(conn, "news_analyses", "text_integrity_status", "TEXT NOT NULL DEFAULT 'not_checked'")
            self._ensure_column(conn, "news_analyses", "validated_fact_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "news_analyses", "rejected_fact_count", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "news_analyses", "fact_validation_notes_json", "TEXT NOT NULL DEFAULT '[]'")
            self._ensure_column(conn, "news_analyses", "integrity_initial_status", "TEXT NOT NULL DEFAULT 'not_checked'")
            self._ensure_column(conn, "news_analyses", "integrity_recovery_status", "TEXT NOT NULL DEFAULT 'not_attempted'")
            self._ensure_column(conn, "news_analyses", "integrity_recovery_model", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "news_analyses", "integrity_recovery_attempts", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "predictions", "evaluation_status", "TEXT NOT NULL DEFAULT 'valid'")
            self._ensure_column(conn, "predictions", "quality_reason", "TEXT")
            self._ensure_column(conn, "predictions", "evaluation_label", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "predictions", "valid_for_directional_trading", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "predictions", "valid_for_probability_calibration", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "event_clusters", "cluster_family", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "event_clusters", "subject_symbol", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(conn, "event_clusters", "target_symbol", "TEXT NOT NULL DEFAULT ''")
            conn.execute("UPDATE predictions SET evaluation_status='invalid' WHERE is_valid=0 AND evaluation_status='valid'")
            conn.execute("UPDATE predictions SET valid_for_directional_trading=1,valid_for_probability_calibration=1 WHERE evaluation_status='valid'")

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migration_applied(self, key: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE migration_key=?", (key,)
            ).fetchone()
            return row is not None

    def _mark_migration(self, key: str, details: str = "") -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations(migration_key,applied_at,details) VALUES (?,?,?)",
                (key, utc_iso(datetime.now(UTC)), details[:1000]),
            )

    def backfill_reaction_governance(self) -> int:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.* FROM news_analyses a
                   JOIN (SELECT news_id,MAX(completed_at) completed_at FROM news_analyses GROUP BY news_id) latest
                     ON latest.news_id=a.news_id AND latest.completed_at=a.completed_at
                   ORDER BY a.news_id,a.id DESC"""
            ).fetchall()
        applied = 0
        seen: set[int] = set()
        for row in rows:
            news_id = int(row["news_id"])
            if news_id in seen:
                continue
            seen.add(news_id)
            try:
                analysis = NewsAnalysis.model_validate_json(str(row["raw_json"]))
            except Exception:
                continue
            usage_status, reason = self.reaction_governance_decision(analysis)
            self.apply_reaction_governance(
                news_id=news_id, analysis_id=int(row["id"]),
                usage_status=usage_status, reason=reason,
            )
            applied += 1
        return applied

    def rebuild_event_clusters(self) -> tuple[int, int]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT a.id analysis_id,a.news_id,a.raw_json,n.headline,n.body,n.published_at
                   FROM news_analyses a JOIN news_articles n ON n.id=a.news_id
                   ORDER BY a.completed_at,a.id"""
            ).fetchall()

        prepared: list[tuple[sqlite3.Row, NewsAnalysis]] = []
        failed = 0
        for row in rows:
            try:
                prepared.append((row, NewsAnalysis.model_validate_json(str(row["raw_json"]))))
            except Exception:
                failed += 1
        # Never destroy an existing derived cluster set when source analyses cannot all be read.
        if failed:
            return 0, failed

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM event_cluster_members")
            conn.execute("DELETE FROM event_clusters")
            conn.execute("UPDATE news_analyses SET event_cluster_id=NULL")
            conn.execute("UPDATE official_documents SET event_cluster_id=NULL")
            conn.execute("COMMIT")

        rebuilt = 0
        for row, analysis in prepared:
            self.assign_event_cluster(
                analysis_id=int(row["analysis_id"]),
                news_id=int(row["news_id"]),
                analysis=analysis,
                headline=str(row["headline"] or ""),
                body=str(row["body"] or ""),
                published_at=str(row["published_at"]),
            )
            rebuilt += 1
        return rebuilt, 0

    def _apply_v011_data_migrations(self) -> None:
        governance_key = "v0.11-reaction-governance"
        if not self._migration_applied(governance_key):
            applied = self.backfill_reaction_governance()
            self._mark_migration(governance_key, f"governed_news={applied}")
        cluster_key = "v0.11-cluster-family-rebuild"
        if not self._migration_applied(cluster_key):
            rebuilt, failed = self.rebuild_event_clusters()
            if failed:
                raise RuntimeError(f"v0.11 cluster rebuild failed for {failed} analysis row(s)")
            self._mark_migration(cluster_key, f"rebuilt={rebuilt}")

    def _apply_v014_attribution_migration(self) -> None:
        """Create an append-only, versioned attribution record for each event.

        Existing ``news_analyses.raw_json`` remains immutable.  The new table holds
        deterministic source-attribution snapshots and permits a later engine
        version or changed article text to be recorded without overwriting history.
        """
        key = "v0.14-event-attributions"
        if self._migration_applied(key):
            return
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_attributions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        news_id INTEGER NOT NULL,
                        attribution_version TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        delivery_symbol TEXT NOT NULL,
                        actual_subject_name TEXT NOT NULL DEFAULT '',
                        actual_subject_symbol TEXT NOT NULL DEFAULT '',
                        actual_subject_asset_type TEXT NOT NULL DEFAULT 'unknown',
                        affected_symbols_json TEXT NOT NULL DEFAULT '[]',
                        benchmark_symbols_json TEXT NOT NULL DEFAULT '[]',
                        attribution_confidence REAL NOT NULL DEFAULT 0,
                        attribution_method TEXT NOT NULL,
                        attribution_status TEXT NOT NULL,
                        attribution_evidence_json TEXT NOT NULL DEFAULT '[]',
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(news_id) REFERENCES news_articles(id),
                        UNIQUE(news_id, attribution_version, source_hash)
                    )
                    """
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_event_attributions_news "
                    "ON event_attributions(news_id, id DESC)"
                )
                conn.execute(
                    "INSERT INTO schema_migrations(migration_key,applied_at,details) VALUES (?,?,?)",
                    (key, utc_iso(datetime.now(UTC)), "created versioned event_attributions table"),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _apply_v014_sec_edgar_migration(self) -> None:
        """Mark the additive SEC official-document metadata migration."""
        key = "v0.14-sec-edgar-official-documents"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "added SEC accession, provenance, raw snapshot, and materiality metadata",
            )

    def _apply_v014_macro_sources_migration(self) -> None:
        key = "v0.14-macro-official-source-documents"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "enabled Federal Reserve, BLS, and BEA official-document ingestion",
            )

    def _apply_v014_historical_events_migration(self) -> None:
        key = "v0.14-historical-events"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created official-document-backed historical event import table",
            )

    def _apply_v014_replay_corpus_migration(self) -> None:
        key = "v0.14-replay-corpus"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created leakage-audited replay corpus table",
            )

    def _apply_v014_replay_market_outcomes_migration(self) -> None:
        key = "v0.14-replay-market-outcomes"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created replay-market outcome table linked to replay corpus rows",
            )

    def _apply_v014_replay_model_scores_migration(self) -> None:
        key = "v0.14-replay-model-scores"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created replay model score table linked to replay outcome rows",
            )

    def _apply_v014_structured_sources_migration(self) -> None:
        key = "v0.14-structured-source-feature-tables"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created SEC financial facts, FRED observations, and technical indicator feature tables",
            )

    def _apply_v014_external_market_signals_migration(self) -> None:
        key = "v0.14-external-market-signals"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created GDELT, Marketaux, and Alpha Vantage geopolitical/news sentiment signal table",
            )
        normalize_key = "v0.14-external-market-signals-alpha-time-normalization"
        if not self._migration_applied(normalize_key):
            with self.connect() as conn:
                updated = conn.execute(
                    """UPDATE external_market_signals
                       SET published_at =
                           substr(published_at,1,4) || '-' ||
                           substr(published_at,5,2) || '-' ||
                           substr(published_at,7,2) || 'T' ||
                           substr(published_at,10,2) || ':' ||
                           substr(published_at,12,2) || ':' ||
                           substr(published_at,14,2) || '+00:00'
                       WHERE source='alpha_vantage'
                         AND length(published_at)=15
                         AND published_at GLOB '????????T??????'"""
                ).rowcount
            self._mark_migration(normalize_key, f"normalized={updated}")

    def _apply_v014_surprise_adjustment_notes_migration(self) -> None:
        key = "v0.14-replay-surprise-adjustment-notes"
        if not self._migration_applied(key):
            self._mark_migration(
                key,
                "created replay surprise adjustment notes for post-outcome scorer diagnosis",
            )

    def _apply_v014_economic_indicator_observations_migration(self) -> None:
        key = "v0.14-economic-indicator-observations"
        if self._migration_applied(key):
            return
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS economic_indicator_observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    canonical_series_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    native_series_id TEXT NOT NULL,
                    observation_date TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL DEFAULT '',
                    frequency TEXT NOT NULL DEFAULT '',
                    region TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    provider_priority INTEGER NOT NULL DEFAULT 50,
                    realtime_start TEXT NOT NULL DEFAULT '',
                    realtime_end TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    source_hash TEXT NOT NULL,
                    ingestion_version TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(canonical_series_id,provider,native_series_id,observation_date,ingestion_version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_economic_indicator_observations_lookup "
                "ON economic_indicator_observations(canonical_series_id,observation_date,provider_priority)"
            )
        self._mark_migration(
            key,
            "created provider-neutral economic indicator observation table for direct official sources",
        )

    def _apply_v014_option_structure_outcome_logs_migration(self) -> None:
        key = "v0.14-option-structure-outcome-logs"
        if self._migration_applied(key):
            return
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS option_structure_outcome_logs (
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
                    updated_at TEXT NOT NULL,
                    UNIQUE(snapshot_id,symbol,horizon,price_source,outcome_version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_option_structure_outcome_logs_lookup "
                "ON option_structure_outcome_logs(symbol,horizon,reaction_at)"
            )
        self._mark_migration(
            key,
            "created M222 option-structure outcome logs for GEX/wall/max-pain learning",
        )

    def _apply_v014_unknown_theme_candidates_migration(self) -> None:
        key = "v0.14-unknown-theme-candidates"
        if self._migration_applied(key):
            return
        with self.connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS unknown_theme_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_source TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    candidate_key TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    provider TEXT NOT NULL DEFAULT '',
                    primary_symbol TEXT NOT NULL DEFAULT '',
                    headline TEXT NOT NULL DEFAULT '',
                    mapping_confidence TEXT NOT NULL DEFAULT '',
                    mapping_reason TEXT NOT NULL DEFAULT '',
                    source_mode TEXT NOT NULL DEFAULT '',
                    tokens_json TEXT NOT NULL DEFAULT '[]',
                    inferred_mapping_json TEXT NOT NULL DEFAULT '{}',
                    provisional_asset_map_json TEXT NOT NULL DEFAULT '[]',
                    market_reaction_json TEXT NOT NULL DEFAULT '{}',
                    ai_review_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'queued',
                    discovery_version TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_source, source_id, discovery_version)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_unknown_theme_candidates_lookup "
                "ON unknown_theme_candidates(status,candidate_key,observed_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_unknown_theme_candidates_time "
                "ON unknown_theme_candidates(observed_at)"
            )
        self._mark_migration(
            key,
            "created unknown-theme candidate queue for low-confidence news asset mapping",
        )

    @staticmethod
    def _attribution_source_hash(
        *, headline: str, body: str, delivery_symbol: str, available_symbols: Iterable[str]
    ) -> str:
        payload = {
            "headline": str(headline or ""),
            "body": str(body or ""),
            "delivery_symbol": str(delivery_symbol or "").strip().upper(),
            "available_symbols": sorted({str(value).strip().upper() for value in available_symbols if str(value).strip()}),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def record_event_attribution(
        self,
        *,
        news_id: int,
        headline: str,
        body: str,
        available_symbols: Iterable[str],
        decision: AttributionDecision,
    ) -> tuple[int, bool]:
        """Persist a deterministic attribution snapshot without rewriting history."""
        symbols = tuple(str(value).strip().upper() for value in available_symbols if str(value).strip())
        source_hash = self._attribution_source_hash(
            headline=headline,
            body=body,
            delivery_symbol=decision.original_subscription_symbol,
            available_symbols=symbols,
        )
        evidence = [
            {
                "kind": "deterministic_rule",
                "engine_version": ATTRIBUTION_ENGINE_VERSION,
                "reason": decision.reason,
            },
            {
                "kind": "subject",
                "name": decision.subject_name or decision.subject_entity,
                "symbol": decision.subject_symbol,
            },
        ]
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO event_attributions(
                    news_id,attribution_version,source_hash,delivery_symbol,
                    actual_subject_name,actual_subject_symbol,actual_subject_asset_type,
                    affected_symbols_json,benchmark_symbols_json,attribution_confidence,
                    attribution_method,attribution_status,attribution_evidence_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    news_id,
                    ATTRIBUTION_ENGINE_VERSION,
                    source_hash,
                    decision.original_subscription_symbol,
                    decision.subject_name or decision.subject_entity,
                    decision.subject_symbol,
                    decision.subject_asset_type,
                    json.dumps(list(decision.affected_symbols), ensure_ascii=False),
                    json.dumps(list(decision.benchmark_symbols), ensure_ascii=False),
                    decision.attribution_confidence,
                    "deterministic_headline_body_rules",
                    decision.attribution_status,
                    json.dumps(evidence, ensure_ascii=False),
                    now,
                ),
            )
            row = conn.execute(
                """SELECT id FROM event_attributions
                   WHERE news_id=? AND attribution_version=? AND source_hash=?""",
                (news_id, ATTRIBUTION_ENGINE_VERSION, source_hash),
            ).fetchone()
        return int(row["id"]), cursor.rowcount == 1

    def attribution_backfill(self, limit: int | None = None) -> dict[str, Any]:
        """Classify stored events once per source snapshot; safe to repeat."""
        sql = "SELECT id,headline,body,primary_symbol FROM news_articles ORDER BY id"
        params: tuple[object, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (max(0, int(limit)),)
        with self.connect() as conn:
            news_rows = conn.execute(sql, params).fetchall()

        created = 0
        status_counts: dict[str, int] = {}
        for news in news_rows:
            news_id = int(news["id"])
            with self.connect() as conn:
                available_symbols = [
                    str(row["symbol"])
                    for row in conn.execute(
                        "SELECT symbol FROM market_baselines WHERE news_id=? ORDER BY symbol",
                        (news_id,),
                    ).fetchall()
                ]
            decision = attribute_event_targets(
                headline=str(news["headline"] or ""),
                body=str(news["body"] or ""),
                original_symbol=str(news["primary_symbol"] or ""),
                available_symbols=available_symbols,
            )
            _, inserted = self.record_event_attribution(
                news_id=news_id,
                headline=str(news["headline"] or ""),
                body=str(news["body"] or ""),
                available_symbols=available_symbols,
                decision=decision,
            )
            created += int(inserted)
            status_counts[decision.attribution_status] = status_counts.get(decision.attribution_status, 0) + 1

        return {
            "scanned": len(news_rows),
            "created": created,
            "unchanged": len(news_rows) - created,
            "status_counts": status_counts,
            "attribution_version": ATTRIBUTION_ENGINE_VERSION,
        }

    def attribution_report(self, limit: int = 50) -> dict[str, Any]:
        latest = """ea.id=(SELECT MAX(current_ea.id) FROM event_attributions current_ea
                          WHERE current_ea.news_id=ea.news_id)"""
        with self.connect() as conn:
            count_rows = conn.execute(
                f"SELECT ea.attribution_status,COUNT(*) count FROM event_attributions ea WHERE {latest} "
                "GROUP BY ea.attribution_status ORDER BY ea.attribution_status"
            ).fetchall()
            rows = conn.execute(
                f"""SELECT ea.*,n.headline,n.published_at,n.received_at
                    FROM event_attributions ea JOIN news_articles n ON n.id=ea.news_id
                    WHERE {latest} ORDER BY ea.id DESC LIMIT ?""",
                (max(0, int(limit)),),
            ).fetchall()
        return {
            "attribution_version": ATTRIBUTION_ENGINE_VERSION,
            "status_counts": {str(row["attribution_status"]): int(row["count"]) for row in count_rows},
            "rows": [dict(row) for row in rows],
        }

    def register_official_candidate(self, news_id: int, source_type: str, source_url: str, title: str = "") -> int:
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO official_documents(
                    news_id,source_type,source_url,title,status,discovered_at,created_at,updated_at
                ) VALUES (?,?,?,?,'candidate',?,?,?)""",
                (news_id, source_type, source_url, title, now, now, now),
            )
            row = conn.execute(
                "SELECT id FROM official_documents WHERE news_id=? AND source_url=?",
                (news_id, source_url),
            ).fetchone()
        return int(row["id"])

    def update_official_document(
        self,
        document_id: int,
        *,
        content: str | None,
        content_hash: str | None,
        status: str,
        last_error: str | None = None,
    ) -> None:
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute(
                """UPDATE official_documents SET content=?,content_hash=?,status=?,last_error=?,
                    fetched_at=CASE WHEN ? IN ('fetched','manual') THEN ? ELSE fetched_at END,updated_at=?
                    WHERE id=?""",
                (content, content_hash, status, last_error, status, now, now, document_id),
            )

    def add_manual_official_document(self, news_id: int, source_type: str, source_url: str, title: str, content: str) -> int:
        import hashlib

        doc_id = self.register_official_candidate(news_id, source_type, source_url, title)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.update_official_document(doc_id, content=content, content_hash=digest, status="manual")
        return doc_id

    def register_sec_filing(self, filing: Any) -> tuple[int, bool]:
        """Create one SEC official-document row per immutable accession number."""
        now = utc_iso(datetime.now(UTC))
        accepted_at = getattr(filing, "accepted_at", None)
        first_seen_at = now
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO official_documents(
                    source_type,source_url,title,document_type,actual_subject_symbol,
                    published_at,accepted_at,first_seen_at,available_at,external_document_id,
                    accession_number,form_type,cik,company_name,ticker,filing_date,primary_document,
                    is_amended,ingestion_version,status,discovered_at,created_at,updated_at
                ) VALUES (
                    'sec',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                    'candidate',?,?,?
                )""",
                (
                    str(getattr(filing, "document_url")),
                    f"{getattr(filing, 'form_type')} {getattr(filing, 'company_name')}",
                    str(getattr(filing, "form_type") or ""),
                    str(getattr(filing, "ticker") or "").upper(),
                    accepted_at,
                    accepted_at,
                    first_seen_at,
                    accepted_at,
                    str(getattr(filing, "accession_number") or ""),
                    str(getattr(filing, "accession_number") or ""),
                    str(getattr(filing, "form_type") or ""),
                    str(getattr(filing, "cik") or ""),
                    str(getattr(filing, "company_name") or ""),
                    str(getattr(filing, "ticker") or "").upper(),
                    str(getattr(filing, "filing_date") or ""),
                    str(getattr(filing, "primary_document") or ""),
                    int(bool(getattr(filing, "is_amended", False))),
                    "sec-edgar-v1",
                    now,
                    now,
                    now,
                ),
            )
            row = conn.execute(
                """SELECT id,first_seen_at FROM official_documents
                   WHERE source_type='sec' AND accession_number=?""",
                (str(getattr(filing, "accession_number") or ""),),
            ).fetchone()
            if row is None:
                raise RuntimeError("SEC filing registration did not return a document row")
            return int(row["id"]), conn.total_changes > before

    def store_sec_filing_content(
        self,
        document_id: int,
        *,
        filing: Any,
        payload: Any,
        classification: Any,
    ) -> None:
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute(
                """UPDATE official_documents
                   SET content=?,content_hash=?,raw_snapshot=?,event_type=?,
                       materiality_classification=?,materiality_reason=?,status='fetched',
                       last_error=NULL,fetched_at=COALESCE(fetched_at,?),updated_at=?
                   WHERE id=? AND source_type='sec'""",
                (
                    str(getattr(payload, "content")),
                    str(getattr(payload, "content_hash")),
                    str(getattr(payload, "raw_snapshot")),
                    str(getattr(classification, "event_type")),
                    str(getattr(classification, "materiality_classification")),
                    str(getattr(classification, "reason")),
                    now,
                    now,
                    document_id,
                ),
            )

    def mark_sec_filing_error(self, document_id: int, error: str) -> None:
        now = utc_iso(datetime.now(UTC))
        with self.connect() as conn:
            conn.execute(
                """UPDATE official_documents
                   SET status='error',last_error=?,updated_at=?
                   WHERE id=? AND source_type='sec'""",
                (error[:1000], now, document_id),
            )

    def upsert_official_document(self, document: Any) -> tuple[int, bool]:
        """Store a public official document snapshot by source-scoped external ID."""
        now = utc_iso(datetime.now(UTC))
        params = {
            "source_type": str(getattr(document, "source_type") or "official"),
            "source_url": str(getattr(document, "source_url") or ""),
            "title": str(getattr(document, "title") or ""),
            "content": str(getattr(document, "content") or ""),
            "content_hash": str(getattr(document, "content_hash") or ""),
            "document_type": str(getattr(document, "document_type") or ""),
            "event_type": str(getattr(document, "event_type") or ""),
            "actual_subject_symbol": str(getattr(document, "actual_subject_symbol") or ""),
            "published_at": getattr(document, "published_at", None),
            "accepted_at": getattr(document, "accepted_at", None),
            "first_seen_at": getattr(document, "first_seen_at", None) or now,
            "available_at": getattr(document, "available_at", None) or getattr(document, "published_at", None) or now,
            "external_document_id": str(getattr(document, "external_document_id") or ""),
            "raw_snapshot": getattr(document, "raw_snapshot", None),
            "materiality_classification": str(getattr(document, "materiality_classification") or "unclassified"),
            "materiality_reason": str(getattr(document, "materiality_reason") or ""),
            "ingestion_version": str(getattr(document, "ingestion_version") or ""),
            "status": str(getattr(document, "status") or "fetched"),
            "discovered_at": now,
            "fetched_at": now,
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO official_documents(
                    source_type,source_url,title,content,content_hash,document_type,event_type,
                    actual_subject_symbol,published_at,accepted_at,first_seen_at,available_at,
                    external_document_id,raw_snapshot,materiality_classification,materiality_reason,
                    ingestion_version,status,discovered_at,fetched_at,created_at,updated_at
                ) VALUES (
                    :source_type,:source_url,:title,:content,:content_hash,:document_type,:event_type,
                    :actual_subject_symbol,:published_at,:accepted_at,:first_seen_at,:available_at,
                    :external_document_id,:raw_snapshot,:materiality_classification,:materiality_reason,
                    :ingestion_version,:status,:discovered_at,:fetched_at,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM official_documents
                   WHERE source_type=? AND external_document_id=?""",
                (params["source_type"], params["external_document_id"]),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    """SELECT id FROM official_documents
                       WHERE source_type=? AND source_url=? ORDER BY id DESC LIMIT 1""",
                    (params["source_type"], params["source_url"]),
                ).fetchone()
            if row is None:
                raise RuntimeError("official document insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE official_documents
                       SET title=?,document_type=?,event_type=?,materiality_classification=?,
                           materiality_reason=?,status=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["title"],
                        params["document_type"],
                        params["event_type"],
                        params["materiality_classification"],
                        params["materiality_reason"],
                        params["status"],
                        now,
                        int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def sec_filing_needs_fetch(self, document_id: int) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT status,content_hash FROM official_documents
                   WHERE id=? AND source_type='sec'""",
                (document_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Unknown SEC official document #{document_id}")
        return str(row["status"]) != "fetched" or not str(row["content_hash"] or "")

    def official_documents_for_news(self, news_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM official_documents WHERE news_id=? ORDER BY CASE status WHEN 'manual' THEN 1 WHEN 'fetched' THEN 2 ELSE 3 END,id",
                (news_id,),
            ).fetchall()]

    def recent_sec_documents(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """SELECT id,actual_subject_symbol,company_name,form_type,accession_number,
                              filing_date,accepted_at,available_at,event_type,
                              materiality_classification,status,source_url,last_error
                       FROM official_documents
                       WHERE source_type='sec'
                       ORDER BY COALESCE(available_at,first_seen_at,created_at) DESC,id DESC
                       LIMIT ?""",
                    (max(0, int(limit)),),
                ).fetchall()
            ]

    def recent_official_documents(self, limit: int = 20, source_type: str | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...]
        if source_type:
            where = "WHERE source_type=?"
            params = (source_type, max(0, int(limit)))
        else:
            params = (max(0, int(limit)),)
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT id,source_type,document_type,event_type,actual_subject_symbol,title,source_url,
                               published_at,accepted_at,first_seen_at,available_at,
                               external_document_id,content_hash,materiality_classification,
                               status,last_error
                        FROM official_documents
                        {where}
                        ORDER BY COALESCE(available_at,first_seen_at,created_at) DESC,id DESC
                        LIMIT ?""",
                    params,
                ).fetchall()
            ]

    def official_document_by_id(self, document_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM official_documents WHERE id=?",
                (int(document_id),),
            ).fetchone()
            return dict(row) if row is not None else None

    def upsert_historical_event(self, event: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "event_type": str(getattr(event, "event_type")),
            "event_subtype": str(getattr(event, "event_subtype") or ""),
            "actual_subject_symbol": str(getattr(event, "actual_subject_symbol") or "").upper(),
            "official_document_id": int(getattr(event, "official_document_id")),
            "event_timestamp": str(getattr(event, "event_timestamp")),
            "available_at": str(getattr(event, "available_at")),
            "source_hash": str(getattr(event, "source_hash")),
            "prediction_cutoff": str(getattr(event, "prediction_cutoff")),
            "market_session": str(getattr(event, "market_session") or "unknown"),
            "replay_eligibility": str(getattr(event, "replay_eligibility") or "review_required"),
            "replay_eligibility_reasons_json": json.dumps(
                list(getattr(event, "replay_eligibility_reasons", ())),
                ensure_ascii=False,
            ),
            "ingestion_version": str(getattr(event, "ingestion_version")),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO historical_events(
                    event_type,event_subtype,actual_subject_symbol,official_document_id,
                    event_timestamp,available_at,source_hash,prediction_cutoff,market_session,
                    replay_eligibility,replay_eligibility_reasons_json,ingestion_version,
                    created_at,updated_at
                ) VALUES (
                    :event_type,:event_subtype,:actual_subject_symbol,:official_document_id,
                    :event_timestamp,:available_at,:source_hash,:prediction_cutoff,:market_session,
                    :replay_eligibility,:replay_eligibility_reasons_json,:ingestion_version,
                    :created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM historical_events
                   WHERE event_type=? AND actual_subject_symbol=? AND official_document_id=?""",
                (
                    params["event_type"],
                    params["actual_subject_symbol"],
                    params["official_document_id"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("historical event insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE historical_events
                       SET event_subtype=?,event_timestamp=?,available_at=?,source_hash=?,
                           prediction_cutoff=?,market_session=?,replay_eligibility=?,
                           replay_eligibility_reasons_json=?,ingestion_version=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["event_subtype"],
                        params["event_timestamp"],
                        params["available_at"],
                        params["source_hash"],
                        params["prediction_cutoff"],
                        params["market_session"],
                        params["replay_eligibility"],
                        params["replay_eligibility_reasons_json"],
                        params["ingestion_version"],
                        now,
                        int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_historical_events(self, limit: int = 20, event_type: str | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...]
        if event_type:
            where = "WHERE he.event_type=?"
            params = (event_type, max(0, int(limit)))
        else:
            params = (max(0, int(limit)),)
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT he.*,od.source_url,od.title,od.accession_number,od.form_type,
                               od.materiality_classification
                        FROM historical_events he
                        JOIN official_documents od ON od.id=he.official_document_id
                        {where}
                        ORDER BY he.available_at DESC,he.id DESC
                        LIMIT ?""",
                    params,
                ).fetchall()
            ]

    def historical_event_candidates(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        event_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if event_type:
            where.append("he.event_type=?")
            params.append(event_type)
        if start:
            where.append("he.available_at>=?")
            params.append(start)
        if end:
            where.append("he.available_at<?")
            params.append(end)
        where_sql = "" if not where else "WHERE " + " AND ".join(where)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT he.*,od.source_type,od.source_url,od.title,od.content,
                               od.content_hash AS official_content_hash,
                               od.status AS official_status,od.accepted_at,od.available_at AS official_available_at,
                               od.accession_number,od.form_type,od.cik,od.company_name,od.ticker,
                               od.materiality_classification,od.materiality_reason
                        FROM historical_events he
                        JOIN official_documents od ON od.id=he.official_document_id
                        {where_sql}
                        ORDER BY he.available_at,he.id
                        LIMIT ?""",
                    params,
                ).fetchall()
            ]

    def upsert_replay_corpus_row(self, row_data: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "corpus_uuid": str(getattr(row_data, "corpus_uuid")),
            "historical_event_id": int(getattr(row_data, "historical_event_id")),
            "official_document_id": int(getattr(row_data, "official_document_id")),
            "actual_subject_symbol": str(getattr(row_data, "actual_subject_symbol") or "").upper(),
            "event_type": str(getattr(row_data, "event_type")),
            "event_subtype": str(getattr(row_data, "event_subtype") or ""),
            "replay_timestamp": str(getattr(row_data, "replay_timestamp")),
            "prediction_cutoff_at": str(getattr(row_data, "prediction_cutoff_at")),
            "session": str(getattr(row_data, "session") or "unknown"),
            "input_snapshot_hash": str(getattr(row_data, "input_snapshot_hash")),
            "input_snapshot_json": str(getattr(row_data, "input_snapshot_json")),
            "timestamp_quality": str(getattr(row_data, "timestamp_quality") or "source_accepted"),
            "source_quality": str(getattr(row_data, "source_quality") or "official_primary"),
            "leakage_status": str(getattr(row_data, "leakage_status") or "pending"),
            "leakage_reasons_json": json.dumps(list(getattr(row_data, "leakage_reasons", ())), ensure_ascii=False),
            "eligibility_status": str(getattr(row_data, "eligibility_status") or "review_required"),
            "blocker_reason": str(getattr(row_data, "blocker_reason") or ""),
            "ingestion_version": str(getattr(row_data, "ingestion_version")),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO replay_corpus(
                    corpus_uuid,historical_event_id,official_document_id,actual_subject_symbol,
                    event_type,event_subtype,replay_timestamp,prediction_cutoff_at,session,
                    input_snapshot_hash,input_snapshot_json,timestamp_quality,source_quality,
                    leakage_status,leakage_reasons_json,eligibility_status,blocker_reason,
                    ingestion_version,created_at,updated_at
                ) VALUES (
                    :corpus_uuid,:historical_event_id,:official_document_id,:actual_subject_symbol,
                    :event_type,:event_subtype,:replay_timestamp,:prediction_cutoff_at,:session,
                    :input_snapshot_hash,:input_snapshot_json,:timestamp_quality,:source_quality,
                    :leakage_status,:leakage_reasons_json,:eligibility_status,:blocker_reason,
                    :ingestion_version,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                "SELECT id FROM replay_corpus WHERE historical_event_id=? AND ingestion_version=?",
                (params["historical_event_id"], params["ingestion_version"]),
            ).fetchone()
            if row is None:
                raise RuntimeError("replay corpus insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE replay_corpus
                       SET corpus_uuid=?,official_document_id=?,actual_subject_symbol=?,
                           event_type=?,event_subtype=?,replay_timestamp=?,prediction_cutoff_at=?,
                           session=?,input_snapshot_hash=?,input_snapshot_json=?,
                           timestamp_quality=?,source_quality=?,leakage_status=?,
                           leakage_reasons_json=?,eligibility_status=?,blocker_reason=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["corpus_uuid"],
                        params["official_document_id"],
                        params["actual_subject_symbol"],
                        params["event_type"],
                        params["event_subtype"],
                        params["replay_timestamp"],
                        params["prediction_cutoff_at"],
                        params["session"],
                        params["input_snapshot_hash"],
                        params["input_snapshot_json"],
                        params["timestamp_quality"],
                        params["source_quality"],
                        params["leakage_status"],
                        params["leakage_reasons_json"],
                        params["eligibility_status"],
                        params["blocker_reason"],
                        now,
                        int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_replay_corpus(self, limit: int = 20, event_type: str | None = None) -> list[dict[str, Any]]:
        where = ""
        params: tuple[Any, ...]
        if event_type:
            where = "WHERE event_type=?"
            params = (event_type, max(0, int(limit)))
        else:
            params = (max(0, int(limit)),)
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM replay_corpus
                        {where}
                        ORDER BY replay_timestamp DESC,id DESC
                        LIMIT ?""",
                    params,
                ).fetchall()
            ]

    def replay_market_outcome_candidates(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        event_type: str | None = None,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        where = ["rc.eligibility_status='eligible'", "rc.leakage_status='pass'"]
        params: list[Any] = []
        if event_type:
            where.append("rc.event_type=?")
            params.append(event_type)
        if start:
            where.append("rc.replay_timestamp>=?")
            params.append(start)
        if end:
            where.append("rc.replay_timestamp<?")
            params.append(end)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT rc.*,he.market_session,he.available_at AS historical_available_at
                        FROM replay_corpus rc
                        JOIN historical_events he ON he.id=rc.historical_event_id
                        WHERE {' AND '.join(where)}
                        ORDER BY rc.replay_timestamp,rc.id
                        LIMIT ?""",
                    params,
                ).fetchall()
            ]

    def upsert_replay_market_outcome(self, outcome: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "replay_corpus_id": int(getattr(outcome, "replay_corpus_id")),
            "historical_event_id": int(getattr(outcome, "historical_event_id")),
            "symbol": str(getattr(outcome, "symbol") or "").upper(),
            "benchmark_symbol": str(getattr(outcome, "benchmark_symbol") or "").upper(),
            "horizon": str(getattr(outcome, "horizon")),
            "baseline_at": getattr(outcome, "baseline_at", None),
            "reaction_at": getattr(outcome, "reaction_at", None),
            "baseline_price": getattr(outcome, "baseline_price", None),
            "reaction_price": getattr(outcome, "reaction_price", None),
            "raw_return": getattr(outcome, "raw_return", None),
            "benchmark_baseline_price": getattr(outcome, "benchmark_baseline_price", None),
            "benchmark_reaction_price": getattr(outcome, "benchmark_reaction_price", None),
            "benchmark_return": getattr(outcome, "benchmark_return", None),
            "excess_return": getattr(outcome, "excess_return", None),
            "direction_label": str(getattr(outcome, "direction_label") or "unknown"),
            "price_source": str(getattr(outcome, "price_source")),
            "price_status": str(getattr(outcome, "price_status") or "pending"),
            "blocker_reason": str(getattr(outcome, "blocker_reason") or ""),
            "ingestion_version": str(getattr(outcome, "ingestion_version")),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO replay_market_outcomes(
                    replay_corpus_id,historical_event_id,symbol,benchmark_symbol,horizon,
                    baseline_at,reaction_at,baseline_price,reaction_price,raw_return,
                    benchmark_baseline_price,benchmark_reaction_price,benchmark_return,
                    excess_return,direction_label,price_source,price_status,blocker_reason,
                    ingestion_version,created_at,updated_at
                ) VALUES (
                    :replay_corpus_id,:historical_event_id,:symbol,:benchmark_symbol,:horizon,
                    :baseline_at,:reaction_at,:baseline_price,:reaction_price,:raw_return,
                    :benchmark_baseline_price,:benchmark_reaction_price,:benchmark_return,
                    :excess_return,:direction_label,:price_source,:price_status,:blocker_reason,
                    :ingestion_version,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM replay_market_outcomes
                   WHERE replay_corpus_id=? AND horizon=? AND price_source=? AND ingestion_version=?""",
                (
                    params["replay_corpus_id"],
                    params["horizon"],
                    params["price_source"],
                    params["ingestion_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("replay market outcome insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE replay_market_outcomes
                       SET historical_event_id=?,symbol=?,benchmark_symbol=?,
                           baseline_at=?,reaction_at=?,baseline_price=?,reaction_price=?,
                           raw_return=?,benchmark_baseline_price=?,benchmark_reaction_price=?,
                           benchmark_return=?,excess_return=?,direction_label=?,
                           price_status=?,blocker_reason=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["historical_event_id"],
                        params["symbol"],
                        params["benchmark_symbol"],
                        params["baseline_at"],
                        params["reaction_at"],
                        params["baseline_price"],
                        params["reaction_price"],
                        params["raw_return"],
                        params["benchmark_baseline_price"],
                        params["benchmark_reaction_price"],
                        params["benchmark_return"],
                        params["excess_return"],
                        params["direction_label"],
                        params["price_status"],
                        params["blocker_reason"],
                        now,
                        int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_replay_market_outcomes(
        self,
        limit: int = 20,
        event_type: str | None = None,
        price_source: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts: list[str] = []
        params_list: list[Any] = []
        if event_type:
            where_parts.append("rc.event_type=?")
            params_list.append(event_type)
        if price_source:
            where_parts.append("rmo.price_source=?")
            params_list.append(price_source)
        where = "" if not where_parts else "WHERE " + " AND ".join(where_parts)
        params_list.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT rmo.*,rc.event_type,rc.event_subtype,rc.replay_timestamp
                        FROM replay_market_outcomes rmo
                        JOIN replay_corpus rc ON rc.id=rmo.replay_corpus_id
                        {where}
                        ORDER BY COALESCE(rmo.reaction_at,rc.replay_timestamp) DESC,rmo.id DESC
                        LIMIT ?""",
                    tuple(params_list),
                ).fetchall()
            ]

    def upsert_option_structure_outcome_log(self, log: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "snapshot_id": str(getattr(log, "snapshot_id") or ""),
            "symbol": str(getattr(log, "symbol") or "").upper(),
            "source_quote_generated_at": str(getattr(log, "source_quote_generated_at") or ""),
            "structure_generated_at": str(getattr(log, "structure_generated_at") or ""),
            "horizon": str(getattr(log, "horizon") or "").upper(),
            "baseline_at": getattr(log, "baseline_at", None),
            "reaction_at": getattr(log, "reaction_at", None),
            "baseline_price": getattr(log, "baseline_price", None),
            "reaction_price": getattr(log, "reaction_price", None),
            "raw_return": getattr(log, "raw_return", None),
            "direction_label": str(getattr(log, "direction_label") or "unknown"),
            "price_source": str(getattr(log, "price_source") or ""),
            "price_status": str(getattr(log, "price_status") or "pending"),
            "blocker_reason": str(getattr(log, "blocker_reason") or ""),
            "gamma_regime": str(getattr(log, "gamma_regime") or ""),
            "total_gamma_exposure": getattr(log, "total_gamma_exposure", None),
            "gamma_flip": getattr(log, "gamma_flip", None),
            "call_wall": getattr(log, "call_wall", None),
            "put_wall": getattr(log, "put_wall", None),
            "max_pain": getattr(log, "max_pain", None),
            "distance_to_pin_pct": getattr(log, "distance_to_pin_pct", None),
            "pin_risk": 1 if bool(getattr(log, "pin_risk", False)) else 0,
            "proximity_alerts_json": json.dumps(getattr(log, "proximity_alerts", []) or [], ensure_ascii=False, sort_keys=True),
            "expiry_bucket_summary_json": json.dumps(getattr(log, "expiry_bucket_summary", []) or [], ensure_ascii=False, sort_keys=True),
            "confidence": str(getattr(log, "confidence") or ""),
            "confidence_score": getattr(log, "confidence_score", None),
            "warnings_json": json.dumps(getattr(log, "warnings", []) or [], ensure_ascii=False, sort_keys=True),
            "structure_json": json.dumps(getattr(log, "structure", {}) or {}, ensure_ascii=False, sort_keys=True),
            "outcome_version": str(getattr(log, "outcome_version") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO option_structure_outcome_logs(
                    snapshot_id,symbol,source_quote_generated_at,structure_generated_at,horizon,
                    baseline_at,reaction_at,baseline_price,reaction_price,raw_return,
                    direction_label,price_source,price_status,blocker_reason,gamma_regime,
                    total_gamma_exposure,gamma_flip,call_wall,put_wall,max_pain,
                    distance_to_pin_pct,pin_risk,proximity_alerts_json,expiry_bucket_summary_json,
                    confidence,confidence_score,warnings_json,structure_json,outcome_version,
                    created_at,updated_at
                ) VALUES (
                    :snapshot_id,:symbol,:source_quote_generated_at,:structure_generated_at,:horizon,
                    :baseline_at,:reaction_at,:baseline_price,:reaction_price,:raw_return,
                    :direction_label,:price_source,:price_status,:blocker_reason,:gamma_regime,
                    :total_gamma_exposure,:gamma_flip,:call_wall,:put_wall,:max_pain,
                    :distance_to_pin_pct,:pin_risk,:proximity_alerts_json,:expiry_bucket_summary_json,
                    :confidence,:confidence_score,:warnings_json,:structure_json,:outcome_version,
                    :created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM option_structure_outcome_logs
                   WHERE snapshot_id=? AND symbol=? AND horizon=? AND price_source=? AND outcome_version=?""",
                (
                    params["snapshot_id"],
                    params["symbol"],
                    params["horizon"],
                    params["price_source"],
                    params["outcome_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("option-structure outcome log insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE option_structure_outcome_logs
                       SET source_quote_generated_at=?,structure_generated_at=?,
                           baseline_at=?,reaction_at=?,baseline_price=?,reaction_price=?,
                           raw_return=?,direction_label=?,price_status=?,blocker_reason=?,
                           gamma_regime=?,total_gamma_exposure=?,gamma_flip=?,call_wall=?,
                           put_wall=?,max_pain=?,distance_to_pin_pct=?,pin_risk=?,
                           proximity_alerts_json=?,expiry_bucket_summary_json=?,
                           confidence=?,confidence_score=?,warnings_json=?,structure_json=?,
                           updated_at=?
                       WHERE id=?""",
                    (
                        params["source_quote_generated_at"],
                        params["structure_generated_at"],
                        params["baseline_at"],
                        params["reaction_at"],
                        params["baseline_price"],
                        params["reaction_price"],
                        params["raw_return"],
                        params["direction_label"],
                        params["price_status"],
                        params["blocker_reason"],
                        params["gamma_regime"],
                        params["total_gamma_exposure"],
                        params["gamma_flip"],
                        params["call_wall"],
                        params["put_wall"],
                        params["max_pain"],
                        params["distance_to_pin_pct"],
                        params["pin_risk"],
                        params["proximity_alerts_json"],
                        params["expiry_bucket_summary_json"],
                        params["confidence"],
                        params["confidence_score"],
                        params["warnings_json"],
                        params["structure_json"],
                        now,
                        int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_option_structure_outcome_logs(
        self,
        *,
        limit: int = 50,
        symbol: str | None = None,
        price_source: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts: list[str] = []
        params: list[Any] = []
        if symbol:
            where_parts.append("symbol=?")
            params.append(symbol.strip().upper())
        if price_source:
            where_parts.append("price_source=?")
            params.append(price_source)
        where = "" if not where_parts else "WHERE " + " AND ".join(where_parts)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM option_structure_outcome_logs
                        {where}
                        ORDER BY COALESCE(reaction_at,source_quote_generated_at,structure_generated_at) DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_outcome_comparison_rows(
        self,
        *,
        limit: int = 20,
        event_type: str | None = None,
        symbol: str | None = None,
        price_source: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts: list[str] = []
        params: list[Any] = []
        if event_type:
            where_parts.append("rc.event_type=?")
            params.append(event_type)
        if symbol:
            where_parts.append("rc.actual_subject_symbol=?")
            params.append(symbol.strip().upper())
        where = "" if not where_parts else "WHERE " + " AND ".join(where_parts)
        params.append(max(0, int(limit)))
        outcome_filter = ""
        if price_source:
            outcome_filter = "AND rmo.price_source=?"
            params.append(price_source)
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""WITH selected AS (
                            SELECT rc.id,rc.actual_subject_symbol,rc.event_type,rc.event_subtype,
                                   rc.replay_timestamp,rc.session
                            FROM replay_corpus rc
                            {where}
                            ORDER BY rc.replay_timestamp DESC,rc.id DESC
                            LIMIT ?
                        )
                        SELECT selected.id AS replay_corpus_id,
                               selected.actual_subject_symbol,
                               selected.event_type,
                               selected.event_subtype,
                               selected.replay_timestamp,
                               selected.session,
                               rmo.id AS outcome_id,
                               rmo.horizon,
                               rmo.price_source,
                               rmo.price_status,
                               rmo.baseline_at,
                               rmo.reaction_at,
                               rmo.baseline_price,
                               rmo.reaction_price,
                               rmo.raw_return,
                               rmo.benchmark_symbol,
                               rmo.benchmark_return,
                               rmo.excess_return,
                               rmo.direction_label,
                               rmo.blocker_reason
                        FROM selected
                        LEFT JOIN replay_market_outcomes rmo
                          ON rmo.replay_corpus_id=selected.id {outcome_filter}
                        ORDER BY selected.replay_timestamp DESC,selected.id DESC,
                                 rmo.price_source,rmo.horizon""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_dataset_export_rows(
        self,
        *,
        limit: int = 1000,
        event_type: str | None = None,
        symbol: str | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        start: str | None = None,
        end: str | None = None,
        include_blocked: bool = False,
    ) -> list[dict[str, Any]]:
        where_parts = ["rc.leakage_status='pass'", "rc.eligibility_status='eligible'"]
        params: list[Any] = []
        if not include_blocked:
            where_parts.append("rmo.price_status='complete'")
        if event_type:
            where_parts.append("rc.event_type=?")
            params.append(event_type)
        if symbol:
            where_parts.append("rc.actual_subject_symbol=?")
            params.append(symbol.strip().upper())
        if price_source:
            where_parts.append("rmo.price_source=?")
            params.append(price_source)
        if horizon:
            where_parts.append("rmo.horizon=?")
            params.append(horizon.strip().upper())
        if start:
            where_parts.append("rc.replay_timestamp>=?")
            params.append(start)
        if end:
            where_parts.append("rc.replay_timestamp<?")
            params.append(end)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT
                            rc.id AS replay_corpus_id,
                            rc.corpus_uuid,
                            rc.historical_event_id,
                            rc.official_document_id,
                            rc.actual_subject_symbol,
                            rc.event_type,
                            rc.event_subtype,
                            rc.replay_timestamp,
                            rc.prediction_cutoff_at,
                            rc.session,
                            rc.input_snapshot_hash,
                            rc.input_snapshot_json,
                            rc.timestamp_quality,
                            rc.source_quality,
                            rc.leakage_status,
                            rc.eligibility_status,
                            rc.blocker_reason AS replay_blocker_reason,
                            rmo.id AS outcome_id,
                            rmo.horizon,
                            rmo.price_source,
                            rmo.price_status,
                            rmo.baseline_at,
                            rmo.reaction_at,
                            rmo.baseline_price,
                            rmo.reaction_price,
                            rmo.raw_return,
                            rmo.benchmark_symbol,
                            rmo.benchmark_baseline_price,
                            rmo.benchmark_reaction_price,
                            rmo.benchmark_return,
                            rmo.excess_return,
                            rmo.direction_label,
                            rmo.blocker_reason AS outcome_blocker_reason,
                            rmo.ingestion_version AS outcome_ingestion_version
                        FROM replay_corpus rc
                        JOIN replay_market_outcomes rmo ON rmo.replay_corpus_id=rc.id
                        WHERE {' AND '.join(where_parts)}
                        ORDER BY rc.replay_timestamp,rc.id,rmo.price_source,rmo.horizon
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_dataset_rows_by_outcome_ids(self, outcome_ids: Iterable[int]) -> list[dict[str, Any]]:
        normalized_ids = sorted({int(outcome_id) for outcome_id in outcome_ids})
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT
                            rc.id AS replay_corpus_id,
                            rc.corpus_uuid,
                            rc.historical_event_id,
                            rc.official_document_id,
                            rc.actual_subject_symbol,
                            rc.event_type,
                            rc.event_subtype,
                            rc.replay_timestamp,
                            rc.prediction_cutoff_at,
                            rc.session,
                            rc.input_snapshot_hash,
                            rc.input_snapshot_json,
                            rc.timestamp_quality,
                            rc.source_quality,
                            rc.leakage_status,
                            rc.eligibility_status,
                            rc.blocker_reason AS replay_blocker_reason,
                            rmo.id AS outcome_id,
                            rmo.horizon,
                            rmo.price_source,
                            rmo.price_status,
                            rmo.baseline_at,
                            rmo.reaction_at,
                            rmo.baseline_price,
                            rmo.reaction_price,
                            rmo.raw_return,
                            rmo.benchmark_symbol,
                            rmo.benchmark_baseline_price,
                            rmo.benchmark_reaction_price,
                            rmo.benchmark_return,
                            rmo.excess_return,
                            rmo.direction_label,
                            rmo.blocker_reason AS outcome_blocker_reason,
                            rmo.ingestion_version AS outcome_ingestion_version
                        FROM replay_corpus rc
                        JOIN replay_market_outcomes rmo ON rmo.replay_corpus_id=rc.id
                        WHERE rc.leakage_status='pass'
                          AND rc.eligibility_status='eligible'
                          AND rmo.price_status='complete'
                          AND rmo.id IN ({placeholders})
                        ORDER BY rc.replay_timestamp,rc.id,rmo.price_source,rmo.horizon""",
                    tuple(normalized_ids),
                ).fetchall()
            ]

    def upsert_replay_model_score(self, score: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "replay_corpus_id": int(getattr(score, "replay_corpus_id")),
            "outcome_id": int(getattr(score, "outcome_id")),
            "historical_event_id": int(getattr(score, "historical_event_id")),
            "official_document_id": int(getattr(score, "official_document_id")),
            "symbol": str(getattr(score, "symbol") or "").upper(),
            "event_type": str(getattr(score, "event_type")),
            "event_subtype": str(getattr(score, "event_subtype") or ""),
            "replay_timestamp": str(getattr(score, "replay_timestamp")),
            "prediction_cutoff_at": str(getattr(score, "prediction_cutoff_at")),
            "horizon": str(getattr(score, "horizon")).upper(),
            "price_source": str(getattr(score, "price_source")),
            "scorer_name": str(getattr(score, "scorer_name")),
            "model_name": str(getattr(score, "model_name")),
            "scorer_version": str(getattr(score, "scorer_version")),
            "input_snapshot_hash": str(getattr(score, "input_snapshot_hash")),
            "prediction_status": str(getattr(score, "prediction_status") or "complete"),
            "predicted_direction": str(getattr(score, "predicted_direction")),
            "expected_return": float(getattr(score, "expected_return")),
            "bear_return": float(getattr(score, "bear_return")),
            "bull_return": float(getattr(score, "bull_return")),
            "up_probability": float(getattr(score, "up_probability")),
            "flat_probability": float(getattr(score, "flat_probability")),
            "down_probability": float(getattr(score, "down_probability")),
            "confidence": float(getattr(score, "confidence")),
            "rationale": str(getattr(score, "rationale") or ""),
            "raw_json": str(getattr(score, "raw_json") or "{}"),
            "actual_direction": str(getattr(score, "actual_direction") or "unknown"),
            "actual_raw_return": getattr(score, "actual_raw_return", None),
            "actual_excess_return": getattr(score, "actual_excess_return", None),
            "direction_hit": getattr(score, "direction_hit", None),
            "absolute_raw_error": getattr(score, "absolute_raw_error", None),
            "absolute_excess_error": getattr(score, "absolute_excess_error", None),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO replay_model_scores(
                    replay_corpus_id,outcome_id,historical_event_id,official_document_id,
                    symbol,event_type,event_subtype,replay_timestamp,prediction_cutoff_at,
                    horizon,price_source,scorer_name,model_name,scorer_version,
                    input_snapshot_hash,prediction_status,predicted_direction,
                    expected_return,bear_return,bull_return,up_probability,
                    flat_probability,down_probability,confidence,rationale,raw_json,
                    actual_direction,actual_raw_return,actual_excess_return,
                    direction_hit,absolute_raw_error,absolute_excess_error,
                    created_at,updated_at
                ) VALUES (
                    :replay_corpus_id,:outcome_id,:historical_event_id,:official_document_id,
                    :symbol,:event_type,:event_subtype,:replay_timestamp,:prediction_cutoff_at,
                    :horizon,:price_source,:scorer_name,:model_name,:scorer_version,
                    :input_snapshot_hash,:prediction_status,:predicted_direction,
                    :expected_return,:bear_return,:bull_return,:up_probability,
                    :flat_probability,:down_probability,:confidence,:rationale,:raw_json,
                    :actual_direction,:actual_raw_return,:actual_excess_return,
                    :direction_hit,:absolute_raw_error,:absolute_excess_error,
                    :created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM replay_model_scores
                   WHERE outcome_id=? AND scorer_name=? AND model_name=? AND scorer_version=?""",
                (
                    params["outcome_id"],
                    params["scorer_name"],
                    params["model_name"],
                    params["scorer_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("replay model score insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE replay_model_scores
                       SET replay_corpus_id=?,historical_event_id=?,official_document_id=?,
                           symbol=?,event_type=?,event_subtype=?,replay_timestamp=?,
                           prediction_cutoff_at=?,horizon=?,price_source=?,input_snapshot_hash=?,
                           prediction_status=?,predicted_direction=?,expected_return=?,
                           bear_return=?,bull_return=?,up_probability=?,flat_probability=?,
                           down_probability=?,confidence=?,rationale=?,raw_json=?,
                           actual_direction=?,actual_raw_return=?,actual_excess_return=?,
                           direction_hit=?,absolute_raw_error=?,absolute_excess_error=?,
                           updated_at=?
                       WHERE id=?""",
                    (
                        params["replay_corpus_id"],
                        params["historical_event_id"],
                        params["official_document_id"],
                        params["symbol"],
                        params["event_type"],
                        params["event_subtype"],
                        params["replay_timestamp"],
                        params["prediction_cutoff_at"],
                        params["horizon"],
                        params["price_source"],
                        params["input_snapshot_hash"],
                        params["prediction_status"],
                        params["predicted_direction"],
                        params["expected_return"],
                        params["bear_return"],
                        params["bull_return"],
                        params["up_probability"],
                        params["flat_probability"],
                        params["down_probability"],
                        params["confidence"],
                        params["rationale"],
                        params["raw_json"],
                        params["actual_direction"],
                        params["actual_raw_return"],
                        params["actual_excess_return"],
                        params["direction_hit"],
                        params["absolute_raw_error"],
                        params["absolute_excess_error"],
                        now,
                        int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def existing_replay_model_score_outcome_ids(
        self,
        *,
        outcome_ids: Iterable[int],
        scorer_name: str,
        model_name: str,
        scorer_version: str,
    ) -> set[int]:
        normalized_ids = sorted({int(outcome_id) for outcome_id in outcome_ids})
        if not normalized_ids:
            return set()
        placeholders = ",".join("?" for _ in normalized_ids)
        params: list[Any] = [
            scorer_name,
            model_name,
            scorer_version,
            *normalized_ids,
        ]
        with self.connect() as conn:
            return {
                int(row["outcome_id"])
                for row in conn.execute(
                    f"""SELECT outcome_id FROM replay_model_scores
                        WHERE scorer_name=? AND model_name=? AND scorer_version=?
                          AND outcome_id IN ({placeholders})""",
                    tuple(params),
                ).fetchall()
            }

    def recent_replay_model_scores(
        self,
        *,
        limit: int = 50,
        event_type: str | None = None,
        symbol: str | None = None,
        scorer_name: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts: list[str] = []
        params: list[Any] = []
        if event_type:
            where_parts.append("event_type=?")
            params.append(event_type)
        if symbol:
            where_parts.append("symbol=?")
            params.append(symbol.strip().upper())
        if scorer_name:
            where_parts.append("scorer_name=?")
            params.append(scorer_name)
        where = "" if not where_parts else "WHERE " + " AND ".join(where_parts)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM replay_model_scores
                        {where}
                        ORDER BY created_at DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_model_score_rows(
        self,
        *,
        limit: int = 1000,
        event_type: str | None = None,
        symbol: str | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        scorer_name: str | None = None,
        model_name: str | None = None,
        scorer_version: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = ["prediction_status='complete'"]
        params: list[Any] = []
        if event_type:
            where_parts.append("event_type=?")
            params.append(event_type)
        if symbol:
            where_parts.append("symbol=?")
            params.append(symbol.strip().upper())
        if price_source:
            where_parts.append("price_source=?")
            params.append(price_source)
        if horizon:
            where_parts.append("horizon=?")
            params.append(horizon.strip().upper())
        if scorer_name:
            where_parts.append("scorer_name=?")
            params.append(scorer_name)
        if model_name:
            where_parts.append("model_name=?")
            params.append(model_name)
        if scorer_version:
            where_parts.append("scorer_version=?")
            params.append(scorer_version)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM replay_model_scores
                        WHERE {' AND '.join(where_parts)}
                        ORDER BY replay_timestamp,id
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_model_score_analysis_rows(
        self,
        *,
        limit: int = 1000,
        event_type: str | None = None,
        symbols: tuple[str, ...] | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        scorer_name: str | None = None,
        model_name: str | None = None,
        scorer_version: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = ["s.prediction_status='complete'"]
        params: list[Any] = []
        if event_type:
            where_parts.append("s.event_type=?")
            params.append(event_type)
        cleaned_symbols = tuple(item.strip().upper() for item in (symbols or ()) if item.strip())
        if cleaned_symbols:
            where_parts.append(f"s.symbol IN ({','.join('?' for _ in cleaned_symbols)})")
            params.extend(cleaned_symbols)
        if price_source:
            where_parts.append("s.price_source=?")
            params.append(price_source)
        if horizon:
            where_parts.append("s.horizon=?")
            params.append(horizon.strip().upper())
        if scorer_name:
            where_parts.append("s.scorer_name=?")
            params.append(scorer_name)
        if model_name:
            where_parts.append("s.model_name=?")
            params.append(model_name)
        if scorer_version:
            where_parts.append("s.scorer_version=?")
            params.append(scorer_version)
        if start:
            where_parts.append("s.replay_timestamp>=?")
            params.append(start)
        if end:
            where_parts.append("s.replay_timestamp<?")
            params.append(end)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT s.*, c.session AS session
                        FROM replay_model_scores s
                        JOIN replay_corpus c ON c.id=s.replay_corpus_id
                        WHERE {' AND '.join(where_parts)}
                        ORDER BY s.replay_timestamp,s.id
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_surprise_candidate_rows(
        self,
        *,
        limit: int = 1000,
        event_type: str | None = None,
        symbols: tuple[str, ...] | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        scorer_name: str | None = None,
        model_name: str | None = None,
        scorer_version: str | None = None,
        start: str | None = None,
        end: str | None = None,
        misses_only: bool = True,
        min_abs_error: float | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = ["s.prediction_status='complete'", "s.direction_hit IS NOT NULL"]
        params: list[Any] = []
        if misses_only:
            where_parts.append("s.direction_hit=0")
        if min_abs_error is not None:
            where_parts.append("(s.absolute_raw_error>=? OR s.absolute_excess_error>=?)")
            params.extend((float(min_abs_error), float(min_abs_error)))
        if event_type:
            where_parts.append("s.event_type=?")
            params.append(event_type)
        cleaned_symbols = tuple(item.strip().upper() for item in (symbols or ()) if item.strip())
        if cleaned_symbols:
            where_parts.append(f"s.symbol IN ({','.join('?' for _ in cleaned_symbols)})")
            params.extend(cleaned_symbols)
        if price_source:
            where_parts.append("s.price_source=?")
            params.append(price_source)
        if horizon:
            where_parts.append("s.horizon=?")
            params.append(horizon.strip().upper())
        if scorer_name:
            where_parts.append("s.scorer_name=?")
            params.append(scorer_name)
        if model_name:
            where_parts.append("s.model_name=?")
            params.append(model_name)
        if scorer_version:
            where_parts.append("s.scorer_version=?")
            params.append(scorer_version)
        if start:
            where_parts.append("s.replay_timestamp>=?")
            params.append(start)
        if end:
            where_parts.append("s.replay_timestamp<?")
            params.append(end)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT
                            s.id AS score_id,
                            s.*,
                            c.session AS session,
                            c.input_snapshot_json AS input_snapshot_json
                        FROM replay_model_scores s
                        JOIN replay_corpus c ON c.id=s.replay_corpus_id
                        WHERE {' AND '.join(where_parts)}
                        ORDER BY
                            COALESCE(s.absolute_excess_error,s.absolute_raw_error,0) DESC,
                            s.replay_timestamp DESC,
                            s.id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def upsert_replay_surprise_adjustment_note(self, note: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "score_id": int(getattr(note, "score_id")),
            "outcome_id": int(getattr(note, "outcome_id")),
            "replay_corpus_id": int(getattr(note, "replay_corpus_id")),
            "symbol": str(getattr(note, "symbol") or "").upper(),
            "event_type": str(getattr(note, "event_type") or ""),
            "event_subtype": str(getattr(note, "event_subtype") or ""),
            "replay_timestamp": str(getattr(note, "replay_timestamp") or ""),
            "horizon": str(getattr(note, "horizon") or "").upper(),
            "price_source": str(getattr(note, "price_source") or ""),
            "scorer_name": str(getattr(note, "scorer_name") or ""),
            "model_name": str(getattr(note, "model_name") or ""),
            "scorer_version": str(getattr(note, "scorer_version") or ""),
            "predicted_direction": str(getattr(note, "predicted_direction") or ""),
            "actual_direction": str(getattr(note, "actual_direction") or ""),
            "expected_return": float(getattr(note, "expected_return") or 0.0),
            "actual_raw_return": getattr(note, "actual_raw_return", None),
            "actual_excess_return": getattr(note, "actual_excess_return", None),
            "mismatch_type": str(getattr(note, "mismatch_type") or ""),
            "reason_category": str(getattr(note, "reason_category") or ""),
            "reason_summary": str(getattr(note, "reason_summary") or "")[:2000],
            "recommended_adjustment": str(getattr(note, "recommended_adjustment") or "")[:2000],
            "feature_hints_json": json.dumps(getattr(note, "feature_hints") or {}, ensure_ascii=False, default=str),
            "evidence_json": json.dumps(getattr(note, "evidence") or {}, ensure_ascii=False, default=str),
            "analysis_provider": str(getattr(note, "analysis_provider") or "deterministic"),
            "analysis_model": str(getattr(note, "analysis_model") or ""),
            "adjustment_version": str(getattr(note, "adjustment_version") or ""),
            "raw_json": json.dumps(getattr(note, "raw") or {}, ensure_ascii=False, default=str),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO replay_surprise_adjustment_notes(
                    score_id,outcome_id,replay_corpus_id,symbol,event_type,event_subtype,
                    replay_timestamp,horizon,price_source,scorer_name,model_name,scorer_version,
                    predicted_direction,actual_direction,expected_return,actual_raw_return,
                    actual_excess_return,mismatch_type,reason_category,reason_summary,
                    recommended_adjustment,feature_hints_json,evidence_json,analysis_provider,
                    analysis_model,adjustment_version,raw_json,created_at,updated_at
                ) VALUES (
                    :score_id,:outcome_id,:replay_corpus_id,:symbol,:event_type,:event_subtype,
                    :replay_timestamp,:horizon,:price_source,:scorer_name,:model_name,:scorer_version,
                    :predicted_direction,:actual_direction,:expected_return,:actual_raw_return,
                    :actual_excess_return,:mismatch_type,:reason_category,:reason_summary,
                    :recommended_adjustment,:feature_hints_json,:evidence_json,:analysis_provider,
                    :analysis_model,:adjustment_version,:raw_json,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM replay_surprise_adjustment_notes
                   WHERE score_id=? AND analysis_provider=? AND analysis_model=? AND adjustment_version=?""",
                (
                    params["score_id"],
                    params["analysis_provider"],
                    params["analysis_model"],
                    params["adjustment_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("replay surprise adjustment note insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE replay_surprise_adjustment_notes
                       SET outcome_id=?,replay_corpus_id=?,symbol=?,event_type=?,event_subtype=?,
                           replay_timestamp=?,horizon=?,price_source=?,scorer_name=?,model_name=?,
                           scorer_version=?,predicted_direction=?,actual_direction=?,expected_return=?,
                           actual_raw_return=?,actual_excess_return=?,mismatch_type=?,reason_category=?,
                           reason_summary=?,recommended_adjustment=?,feature_hints_json=?,evidence_json=?,
                           raw_json=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["outcome_id"], params["replay_corpus_id"], params["symbol"],
                        params["event_type"], params["event_subtype"], params["replay_timestamp"],
                        params["horizon"], params["price_source"], params["scorer_name"],
                        params["model_name"], params["scorer_version"], params["predicted_direction"],
                        params["actual_direction"], params["expected_return"], params["actual_raw_return"],
                        params["actual_excess_return"], params["mismatch_type"], params["reason_category"],
                        params["reason_summary"], params["recommended_adjustment"],
                        params["feature_hints_json"], params["evidence_json"], params["raw_json"],
                        now, int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_replay_surprise_adjustment_notes(
        self,
        *,
        limit: int = 50,
        scorer_name: str | None = None,
        scorer_version: str | None = None,
        symbol: str | None = None,
        analysis_provider: str | None = None,
        adjustment_version: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts: list[str] = []
        params: list[Any] = []
        if scorer_name:
            where_parts.append("scorer_name=?")
            params.append(scorer_name)
        if scorer_version:
            where_parts.append("scorer_version=?")
            params.append(scorer_version)
        if symbol:
            where_parts.append("symbol=?")
            params.append(symbol.strip().upper())
        if analysis_provider:
            where_parts.append("analysis_provider=?")
            params.append(analysis_provider.strip().lower())
        if adjustment_version:
            where_parts.append("adjustment_version=?")
            params.append(adjustment_version)
        where = "" if not where_parts else "WHERE " + " AND ".join(where_parts)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM replay_surprise_adjustment_notes
                        {where}
                        ORDER BY created_at DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_score_leaderboard_rows(
        self,
        *,
        event_type: str | None = None,
        symbol: str | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        scorer_name: str | None = None,
        min_rows: int = 1,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        where_parts = ["prediction_status='complete'"]
        params: list[Any] = []
        if event_type:
            where_parts.append("event_type=?")
            params.append(event_type)
        if symbol:
            where_parts.append("symbol=?")
            params.append(symbol.strip().upper())
        if price_source:
            where_parts.append("price_source=?")
            params.append(price_source)
        if horizon:
            where_parts.append("horizon=?")
            params.append(horizon.strip().upper())
        if scorer_name:
            where_parts.append("scorer_name=?")
            params.append(scorer_name)
        params.extend((max(1, int(min_rows)), max(0, int(limit))))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT
                            scorer_name,
                            model_name,
                            scorer_version,
                            price_source,
                            horizon,
                            COUNT(*) AS rows,
                            SUM(CASE WHEN direction_hit IS NOT NULL THEN 1 ELSE 0 END) AS evaluated,
                            SUM(CASE WHEN direction_hit=1 THEN 1 ELSE 0 END) AS direction_hits,
                            AVG(CASE WHEN direction_hit IS NOT NULL THEN direction_hit ELSE NULL END) AS direction_accuracy,
                            AVG(absolute_raw_error) AS avg_absolute_raw_error,
                            AVG(absolute_excess_error) AS avg_absolute_excess_error,
                            AVG(confidence) AS avg_confidence,
                            MIN(replay_timestamp) AS first_replay_timestamp,
                            MAX(replay_timestamp) AS last_replay_timestamp,
                            MIN(created_at) AS first_scored_at,
                            MAX(updated_at) AS last_scored_at
                        FROM replay_model_scores
                        WHERE {' AND '.join(where_parts)}
                        GROUP BY scorer_name,model_name,scorer_version,price_source,horizon
                        HAVING COUNT(*) >= ?
                        ORDER BY direction_accuracy DESC,
                                 avg_absolute_raw_error ASC,
                                 rows DESC,
                                 scorer_name,
                                 model_name,
                                 price_source,
                                 horizon
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def replay_score_pair_comparison_rows(
        self,
        *,
        base_scorer_name: str,
        base_scorer_version: str,
        candidate_scorer_name: str,
        candidate_scorer_version: str,
        base_model_name: str | None = None,
        candidate_model_name: str | None = None,
        limit: int = 1000,
        event_type: str | None = None,
        symbols: tuple[str, ...] | None = None,
        price_source: str | None = None,
        horizon: str | None = None,
        start: str | None = None,
        end: str | None = None,
    ) -> list[dict[str, Any]]:
        where_parts = [
            "b.prediction_status='complete'",
            "c.prediction_status='complete'",
            "b.scorer_name=?",
            "b.scorer_version=?",
            "c.scorer_name=?",
            "c.scorer_version=?",
        ]
        params: list[Any] = [
            base_scorer_name,
            base_scorer_version,
            candidate_scorer_name,
            candidate_scorer_version,
        ]
        if base_model_name:
            where_parts.append("b.model_name=?")
            params.append(base_model_name)
        if candidate_model_name:
            where_parts.append("c.model_name=?")
            params.append(candidate_model_name)
        if event_type:
            where_parts.append("b.event_type=?")
            params.append(event_type)
        cleaned_symbols = tuple(item.strip().upper() for item in (symbols or ()) if item.strip())
        if cleaned_symbols:
            where_parts.append(f"b.symbol IN ({','.join('?' for _ in cleaned_symbols)})")
            params.extend(cleaned_symbols)
        if price_source:
            where_parts.append("b.price_source=?")
            params.append(price_source)
        if horizon:
            where_parts.append("b.horizon=?")
            params.append(horizon.strip().upper())
        if start:
            where_parts.append("b.replay_timestamp>=?")
            params.append(start)
        if end:
            where_parts.append("b.replay_timestamp<?")
            params.append(end)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT
                            b.outcome_id,
                            b.replay_corpus_id,
                            b.historical_event_id,
                            b.official_document_id,
                            b.symbol,
                            b.event_type,
                            b.event_subtype,
                            b.replay_timestamp,
                            b.prediction_cutoff_at,
                            b.horizon,
                            b.price_source,
                            rc.session AS session,
                            b.actual_direction,
                            b.actual_raw_return,
                            b.actual_excess_return,
                            b.scorer_name AS base_scorer_name,
                            b.model_name AS base_model_name,
                            b.scorer_version AS base_scorer_version,
                            b.predicted_direction AS base_predicted_direction,
                            b.expected_return AS base_expected_return,
                            b.confidence AS base_confidence,
                            b.direction_hit AS base_direction_hit,
                            b.absolute_raw_error AS base_absolute_raw_error,
                            b.absolute_excess_error AS base_absolute_excess_error,
                            c.scorer_name AS candidate_scorer_name,
                            c.model_name AS candidate_model_name,
                            c.scorer_version AS candidate_scorer_version,
                            c.predicted_direction AS candidate_predicted_direction,
                            c.expected_return AS candidate_expected_return,
                            c.confidence AS candidate_confidence,
                            c.direction_hit AS candidate_direction_hit,
                            c.absolute_raw_error AS candidate_absolute_raw_error,
                            c.absolute_excess_error AS candidate_absolute_excess_error
                        FROM replay_model_scores b
                        JOIN replay_model_scores c
                          ON c.outcome_id=b.outcome_id
                        JOIN replay_corpus rc
                          ON rc.id=b.replay_corpus_id
                        WHERE {' AND '.join(where_parts)}
                        ORDER BY b.replay_timestamp,b.outcome_id,b.id,c.id
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def enqueue_jobs(
        self,
        analysis_key: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        max_age_days: int,
        limit: int,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=max_age_days)
        now_iso = utc_iso(now)
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """
                INSERT OR IGNORE INTO analysis_jobs(
                    news_id, analysis_key, provider, model_name, prompt_version,
                    status, priority, attempts, next_attempt_at, created_at, updated_at
                )
                SELECT n.id, ?, ?, ?, ?, 'queued', 0, 0, ?, ?, ?
                FROM news_articles n
                WHERE n.received_at >= ?
                  AND NOT EXISTS (
                      SELECT 1 FROM analysis_jobs existing
                      WHERE existing.news_id=n.id AND existing.analysis_key=?
                  )
                ORDER BY n.id ASC
                LIMIT ?
                """,
                (
                    analysis_key,
                    provider,
                    model_name,
                    prompt_version,
                    now_iso,
                    now_iso,
                    now_iso,
                    utc_iso(cutoff),
                    analysis_key,
                    limit,
                ),
            )
            return conn.total_changes - before

    def enqueue_news_job(
        self,
        news_id: int,
        analysis_key: str,
        provider: str,
        model_name: str,
        prompt_version: str,
        *,
        priority: int = 100,
        now: datetime | None = None,
    ) -> int:
        now = now or datetime.now(UTC)
        now_iso = utc_iso(now)
        with self.connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO analysis_jobs(
                    news_id,analysis_key,provider,model_name,prompt_version,status,priority,attempts,
                    next_attempt_at,created_at,updated_at
                ) VALUES (?,?,?,?,?,'queued',?,0,?,?,?)""",
                (news_id, analysis_key, provider, model_name, prompt_version, priority, now_iso, now_iso, now_iso),
            )
            row = conn.execute(
                "SELECT id FROM analysis_jobs WHERE news_id=? AND analysis_key=?",
                (news_id, analysis_key),
            ).fetchone()
            return int(row["id"])

    def claim_next_job(
        self,
        worker_id: str,
        analysis_key: str,
        stale_seconds: int,
        body_wait_seconds: int,
        now: datetime | None = None,
    ) -> ClaimedJob | None:
        now = now or datetime.now(UTC)
        now_iso = utc_iso(now)
        stale_cutoff = utc_iso(now - timedelta(seconds=stale_seconds))
        body_ready_cutoff = utc_iso(now - timedelta(seconds=body_wait_seconds))

        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stale_rows = conn.execute(
                """
                SELECT id FROM analysis_jobs
                WHERE analysis_key=? AND status='processing'
                  AND locked_at IS NOT NULL AND locked_at < ?
                """,
                (analysis_key, stale_cutoff),
            ).fetchall()
            if stale_rows:
                stale_ids = [int(row["id"]) for row in stale_rows]
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"""
                    UPDATE analysis_jobs
                    SET status='retry', locked_by=NULL, locked_at=NULL,
                        next_attempt_at=?, last_error='stale worker lease recovered',
                        updated_at=?
                    WHERE id IN ({placeholders})
                    """,
                    (now_iso, now_iso, *stale_ids),
                )
                for job_id in stale_ids:
                    conn.execute(
                        """
                        INSERT INTO analysis_job_events(job_id,event_type,message,created_at)
                        VALUES (?, 'lease_recovered', 'stale worker lease recovered', ?)
                        """,
                        (job_id, now_iso),
                    )

            row = conn.execute(
                """
                SELECT j.id AS job_id, j.news_id AS job_news_id,
                       j.attempts AS job_attempts, j.analysis_key AS job_analysis_key,
                       n.id AS news_row_id, n.provider AS news_provider,
                       n.article_id AS news_article_id, n.headline AS news_headline,
                       n.normalized_headline AS news_normalized_headline,
                       n.headline_hash AS news_headline_hash, n.body AS news_body,
                       n.published_at AS news_published_at,
                       n.received_at AS news_received_at,
                       n.provider_published_at AS news_provider_published_at,
                       n.source_accepted_at AS news_source_accepted_at,
                       n.first_seen_at AS news_first_seen_at,
                       n.content_updated_at AS news_content_updated_at,
                       n.timestamp_quality AS news_timestamp_quality,
                       n.timestamp_source AS news_timestamp_source,
                       n.primary_symbol AS news_primary_symbol,
                       n.con_id AS news_con_id, n.extra_data AS news_extra_data,
                       n.created_at AS news_created_at
                FROM analysis_jobs j
                JOIN news_articles n ON n.id=j.news_id
                WHERE j.analysis_key=?
                  AND j.status IN ('queued','retry')
                  AND j.next_attempt_at<=?
                  AND n.received_at<=?
                ORDER BY j.priority DESC, j.id ASC
                LIMIT 1
                """,
                (analysis_key, now_iso, body_ready_cutoff),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None

            job_id = int(row["job_id"])
            cursor = conn.execute(
                """
                UPDATE analysis_jobs
                SET status='processing', attempts=attempts+1,
                    locked_by=?, locked_at=?, updated_at=?
                WHERE id=? AND status IN ('queued','retry')
                """,
                (worker_id, now_iso, now_iso, job_id),
            )
            if cursor.rowcount != 1:
                conn.execute("ROLLBACK")
                return None
            conn.execute(
                """
                INSERT INTO analysis_job_events(job_id,event_type,message,created_at)
                VALUES (?, 'claimed', ?, ?)
                """,
                (job_id, worker_id, now_iso),
            )
            conn.execute("COMMIT")

            news = {
                "id": row["news_row_id"],
                "provider": row["news_provider"],
                "article_id": row["news_article_id"],
                "headline": row["news_headline"],
                "normalized_headline": row["news_normalized_headline"],
                "headline_hash": row["news_headline_hash"],
                "body": row["news_body"],
                "published_at": row["news_published_at"],
                "received_at": row["news_received_at"],
                "provider_published_at": row["news_provider_published_at"],
                "source_accepted_at": row["news_source_accepted_at"],
                "first_seen_at": row["news_first_seen_at"],
                "content_updated_at": row["news_content_updated_at"],
                "timestamp_quality": row["news_timestamp_quality"],
                "timestamp_source": row["news_timestamp_source"],
                "primary_symbol": row["news_primary_symbol"],
                "con_id": row["news_con_id"],
                "extra_data": row["news_extra_data"],
                "created_at": row["news_created_at"],
            }
            return ClaimedJob(
                job_id=job_id,
                news_id=int(row["job_news_id"]),
                attempts=int(row["job_attempts"]) + 1,
                analysis_key=analysis_key,
                news=news,
            )

    def get_news_context(self, news_id: int) -> tuple[list[str], list[dict[str, Any]]]:
        with self.connect() as conn:
            symbols = [
                str(row["symbol"])
                for row in conn.execute(
                    "SELECT symbol FROM news_symbols WHERE news_id=? ORDER BY symbol",
                    (news_id,),
                ).fetchall()
            ]
            baselines = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT symbol,captured_at,bid,ask,last,midpoint,selected_price,
                           selected_price_source,volume,currency,con_id,contract_symbol,
                           sec_type,exchange,primary_exchange,multiplier,market_data_type,
                           quote_integrity_status,quote_integrity_reasons_json,
                           reaction_baseline_eligible,spread_ratio,last_midpoint_deviation,
                           close_reference_deviation
                    FROM market_baselines WHERE news_id=? ORDER BY symbol
                    """,
                    (news_id,),
                ).fetchall()
            ]
        return symbols, baselines

    def _prediction_due_at(
        self, conn: sqlite3.Connection, news_id: int, symbol: str, horizon: str
    ) -> str | None:
        row = conn.execute(
            """
            SELECT due_at FROM reaction_targets
            WHERE news_id=? AND symbol=? AND lower(horizon)=lower(?)
            LIMIT 1
            """,
            (news_id, symbol, horizon),
        ).fetchone()
        return str(row["due_at"]) if row else None

    @staticmethod
    def reaction_governance_decision(analysis: NewsAnalysis) -> tuple[str, str]:
        if (
            analysis.attribution_status in {"mismatch", "multi_subject", "unknown", "unresolved"}
            or not analysis.subject_target_available
            or analysis.target_role == "unavailable_subject"
        ):
            return "excluded", "actual article subject has no trustworthy event-time target quote"
        if analysis.event_type == "market_price_update":
            return "excluded", "retrospective or descriptive market-price update"
        if analysis.prediction_eligible:
            return "eligible", "fresh prediction-eligible causal event"
        if analysis.reaction_observation_eligible:
            return "observation_only", "observation retained but excluded from prediction calibration"
        return "excluded", "analysis did not authorize reaction observation or calibration"

    def complete_job(
        self,
        *,
        job: ClaimedJob,
        worker_id: str,
        analysis: NewsAnalysis,
        prompt_hash: str,
        input_hash: str,
        input_snapshot_json: str,
        baseline_snapshot_json: str,
        headline_hash: str,
        body_hash: str,
        raw_json: str,
        response_id: str | None,
        provider: str,
        model_name: str,
        prompt_version: str,
        started_at: datetime,
        completed_at: datetime,
        duration_ms: int,
        input_tokens: int | None,
        output_tokens: int | None,
        quality_config: dict[str, Any] | None = None,
    ) -> int:
        now_iso = utc_iso(completed_at)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            lock = conn.execute(
                "SELECT status,locked_by FROM analysis_jobs WHERE id=?",
                (job.job_id,),
            ).fetchone()
            if lock is None:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"Analysis job {job.job_id} no longer exists")
            if str(lock["status"]) != "processing" or str(lock["locked_by"]) != worker_id:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"Worker no longer owns analysis job {job.job_id}")

            existing = conn.execute(
                "SELECT id FROM news_analyses WHERE news_id=? AND analysis_key=?",
                (job.news_id, job.analysis_key),
            ).fetchone()
            if existing:
                analysis_id = int(existing["id"])
            else:
                cursor = conn.execute(
                    """
                    INSERT INTO news_analyses(
                        job_id,news_id,analysis_key,provider,model_name,prompt_version,
                        prompt_hash,input_hash,response_id,started_at,completed_at,duration_ms,
                        event_type,event_subtype,event_scope,subject_entity,event_subject_symbol,subject_asset_type,
                        target_asset_type,target_role,target_assets_json,sentiment,causal_signal,prediction_eligible,
                        reaction_observation_eligible,analysis_priority,primary_symbol,related_symbols_json,impact_score,
                        novelty_score,source_quality,source_tier,official_source_status,confidence,time_horizon,summary,
                        bull_case_json,bear_case_json,data_limitations_json,text_integrity_status,validated_fact_count,
                        rejected_fact_count,fact_validation_notes_json,raw_json,input_tokens,output_tokens,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        job.job_id,
                        job.news_id,
                        job.analysis_key,
                        provider,
                        model_name,
                        prompt_version,
                        prompt_hash,
                        input_hash,
                        response_id,
                        utc_iso(started_at),
                        utc_iso(completed_at),
                        duration_ms,
                        analysis.event_type,
                        analysis.event_subtype,
                        analysis.event_scope,
                        analysis.subject_entity,
                        analysis.event_subject_symbol,
                        analysis.subject_asset_type,
                        analysis.target_asset_type,
                        analysis.target_role,
                        json.dumps(analysis.target_assets, ensure_ascii=False),
                        analysis.sentiment,
                        int(analysis.causal_signal),
                        int(analysis.prediction_eligible),
                        int(analysis.reaction_observation_eligible),
                        analysis.analysis_priority,
                        analysis.primary_symbol,
                        json.dumps(analysis.related_symbols, ensure_ascii=False),
                        analysis.impact_score,
                        analysis.novelty_score,
                        analysis.source_quality,
                        analysis.source_tier,
                        analysis.official_source_status,
                        analysis.confidence,
                        analysis.time_horizon,
                        analysis.summary,
                        json.dumps(analysis.bull_case, ensure_ascii=False),
                        json.dumps(analysis.bear_case, ensure_ascii=False),
                        json.dumps(analysis.data_limitations, ensure_ascii=False),
                        analysis.text_integrity_status,
                        analysis.validated_fact_count,
                        analysis.rejected_fact_count,
                        json.dumps(analysis.fact_validation_notes, ensure_ascii=False),
                        raw_json,
                        input_tokens,
                        output_tokens,
                        now_iso,
                    ),
                )
                analysis_id = int(cursor.lastrowid)

                conn.execute(
                    """UPDATE news_analyses SET integrity_initial_status=?,integrity_recovery_status=?,
                       integrity_recovery_model=?,integrity_recovery_attempts=?,attribution_status=?,
                       attribution_confidence=?,original_subscription_symbol=?,subject_target_available=?,
                       attribution_reason=?,article_scope=?,article_primary_subject=?,mentioned_symbols_json=?,
                       subscription_entity_role=?,target_quote_integrity_status=?,target_quote_integrity_reasons_json=?,
                       delivery_reference_symbol=?,delivery_reference_price=?,delivery_reference_price_source=?
                       WHERE id=?""",
                    (
                        analysis.integrity_initial_status,
                        analysis.integrity_recovery_status,
                        analysis.integrity_recovery_model,
                        analysis.integrity_recovery_attempts,
                        analysis.attribution_status,
                        analysis.attribution_confidence,
                        analysis.original_subscription_symbol,
                        int(analysis.subject_target_available),
                        analysis.attribution_reason,
                        analysis.article_scope,
                        analysis.article_primary_subject,
                        json.dumps(analysis.mentioned_symbols, ensure_ascii=False),
                        analysis.subscription_entity_role,
                        analysis.target_quote_integrity_status,
                        json.dumps(analysis.target_quote_integrity_reasons, ensure_ascii=False),
                        analysis.delivery_reference_symbol,
                        analysis.delivery_reference_price,
                        analysis.delivery_reference_price_source,
                        analysis_id,
                    ),
                )

                conn.execute(
                    """
                    INSERT INTO prediction_inputs(
                        analysis_id,news_id,input_hash,headline_hash,body_hash,
                        input_snapshot_json,baseline_snapshot_json,created_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        analysis_id,
                        job.news_id,
                        input_hash,
                        headline_hash,
                        body_hash,
                        input_snapshot_json,
                        baseline_snapshot_json,
                        now_iso,
                    ),
                )

                for prediction in analysis.predictions:
                    due_at = self._prediction_due_at(
                        conn,
                        job.news_id,
                        prediction.symbol,
                        prediction.horizon,
                    )
                    evaluation_status = "valid"
                    invalid_reason = None
                    quality_reason = None
                    valid_for_directional = 0
                    valid_for_calibration = 0
                    if due_at is None:
                        evaluation_status = "invalid"
                        invalid_reason = "matching reaction target unavailable"
                    elif parse_utc(due_at) <= completed_at.astimezone(UTC):
                        evaluation_status = "invalid"
                        invalid_reason = "prediction completed after evaluation horizon"
                    else:
                        qc = quality_config or {}
                        decision = evaluate_prediction_quality(
                            analysis, prediction,
                            min_analysis_confidence=float(qc.get("min_analysis_confidence", 0.45)),
                            min_prediction_confidence=float(qc.get("min_prediction_confidence", 0.45)),
                            min_top_probability=float(qc.get("min_top_probability", 0.50)),
                            min_probability_edge=float(qc.get("min_probability_edge", 0.08)),
                            max_range_width_by_horizon=qc.get("max_range_width_by_horizon"),
                            official_required_event_types=tuple(qc.get("official_required_event_types", ())),
                            min_validated_facts_without_official=int(qc.get("min_validated_facts_without_official", 2)),
                            integrity_partial_calibration_only=bool(qc.get("integrity_partial_calibration_only", True)),
                        )
                        evaluation_status = decision.status
                        quality_reason = decision.reason
                        valid_for_directional = int(decision.valid_for_directional_trading)
                        valid_for_calibration = int(decision.valid_for_probability_calibration)
                    if evaluation_status == "invalid":
                        valid_for_directional = 0
                        valid_for_calibration = 0
                    is_valid = 1 if evaluation_status == "valid" else 0
                    label = evaluation_label(parse_utc(str(job.news["received_at"])), prediction.horizon)
                    conn.execute(
                        """
                        INSERT INTO predictions(
                            analysis_id,news_id,symbol,horizon,expected_return,
                            bear_return,bull_return,up_probability,flat_probability,
                            down_probability,confidence,created_at,evaluation_due_at,
                            is_valid,invalid_reason,evaluation_status,quality_reason,evaluation_label,
                            valid_for_directional_trading,valid_for_probability_calibration
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            analysis_id,
                            job.news_id,
                            prediction.symbol,
                            prediction.horizon,
                            prediction.expected_return,
                            prediction.bear_return,
                            prediction.bull_return,
                            prediction.up_probability,
                            prediction.flat_probability,
                            prediction.down_probability,
                            prediction.confidence,
                            now_iso,
                            due_at,
                            is_valid,
                            invalid_reason,
                            evaluation_status,
                            quality_reason,
                            label,
                            valid_for_directional,
                            valid_for_calibration,
                        ),
                    )

            usage_status, usage_reason = self.reaction_governance_decision(analysis)
            calibration_eligible = int(usage_status == "eligible")
            observation_eligible = int(usage_status in {"eligible", "observation_only"})
            conn.execute(
                """UPDATE reaction_targets
                   SET usage_status=?,usage_reason=?,governance_analysis_id=?,
                       calibration_eligible=?,observation_eligible=?,updated_at=?,
                       status=CASE
                           WHEN ?='excluded' AND status='pending' THEN 'suppressed'
                           WHEN ?!='excluded' AND status='suppressed' THEN 'pending'
                           ELSE status END
                   WHERE news_id=?""",
                (usage_status, usage_reason, analysis_id, calibration_eligible,
                 observation_eligible, now_iso, usage_status, usage_status, job.news_id),
            )
            conn.execute(
                """UPDATE market_reactions
                   SET usage_status=?,usage_reason=?,governance_analysis_id=?,
                       calibration_eligible=?,observation_eligible=?
                   WHERE news_id=?""",
                (usage_status, usage_reason, analysis_id, calibration_eligible,
                 observation_eligible, job.news_id),
            )

            # Persist a transparent explanation of the final prediction gate
            # decision. This audit is diagnostic-only and never changes eligibility.
            decisions = derive_gate_decisions(
                analysis,
                raw=json.loads(raw_json or "{}"),
                prediction_count=len(analysis.predictions),
                official_required_event_types=tuple(
                    (quality_config or {}).get("official_required_event_types", ())
                ),
            )
            write_gate_audit(
                conn,
                analysis_id=analysis_id,
                news_id=job.news_id,
                decisions=decisions,
                policy_version=DIAGNOSTIC_POLICY_VERSION,
                audit_source="runtime",
                evaluated_at=now_iso,
            )

            conn.execute(
                """
                UPDATE analysis_jobs
                SET status='complete',locked_by=NULL,locked_at=NULL,last_error=NULL,
                    updated_at=? WHERE id=?
                """,
                (now_iso, job.job_id),
            )
            conn.execute(
                """
                INSERT INTO analysis_job_events(job_id,event_type,message,created_at)
                VALUES (?, 'completed', ?, ?)
                """,
                (job.job_id, f"analysis_id={analysis_id}", now_iso),
            )
            conn.execute("COMMIT")
            return analysis_id

    def fail_job(
        self,
        job: ClaimedJob,
        worker_id: str,
        message: str,
        *,
        retry_at: datetime | None,
        permanent: bool,
        max_attempts: int,
        now: datetime | None = None,
    ) -> str:
        now = now or datetime.now(UTC)
        now_iso = utc_iso(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status,locked_by,attempts FROM analysis_jobs WHERE id=?",
                (job.job_id,),
            ).fetchone()
            if row is None:
                conn.execute("ROLLBACK")
                return "missing"
            if str(row["status"]) != "processing" or str(row["locked_by"]) != worker_id:
                conn.execute("ROLLBACK")
                return "lost_lease"
            attempts = int(row["attempts"])
            dead = attempts >= max_attempts
            status = "dead" if dead else "retry"
            if permanent and attempts >= min(2, max_attempts):
                status = "dead"
            next_attempt_at = retry_at or now
            conn.execute(
                """
                UPDATE analysis_jobs
                SET status=?,next_attempt_at=?,locked_by=NULL,locked_at=NULL,
                    last_error=?,updated_at=? WHERE id=?
                """,
                (
                    status,
                    utc_iso(next_attempt_at),
                    message[:2000],
                    now_iso,
                    job.job_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO analysis_job_events(job_id,event_type,message,created_at)
                VALUES (?,?,?,?)
                """,
                (
                    job.job_id,
                    "dead" if status == "dead" else "retry_scheduled",
                    message[:2000],
                    now_iso,
                ),
            )
            conn.execute("COMMIT")
            return status

    def reset_dead_jobs(
        self, analysis_key: str, limit: int = 1000, now: datetime | None = None
    ) -> int:
        now = now or datetime.now(UTC)
        now_iso = utc_iso(now)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id FROM analysis_jobs
                WHERE analysis_key=? AND status='dead'
                ORDER BY id LIMIT ?
                """,
                (analysis_key, limit),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""
                    UPDATE analysis_jobs
                    SET status='retry',attempts=0,next_attempt_at=?,last_error=NULL,
                        locked_by=NULL,locked_at=NULL,updated_at=?
                    WHERE id IN ({placeholders})
                    """,
                    (now_iso, now_iso, *ids),
                )
                for job_id in ids:
                    conn.execute(
                        """
                        INSERT INTO analysis_job_events(job_id,event_type,message,created_at)
                        VALUES (?, 'manual_retry', 'dead job reset by operator', ?)
                        """,
                        (job_id, now_iso),
                    )
            conn.execute("COMMIT")
            return len(ids)

    def analysis_summary(self, analysis_key: str | None = None) -> dict[str, int]:
        where = " WHERE analysis_key=?" if analysis_key else ""
        params: tuple[Any, ...] = (analysis_key,) if analysis_key else ()
        with self.connect() as conn:
            result: dict[str, int] = {}
            for status in ("queued", "processing", "retry", "complete", "skipped", "dead"):
                result[f"jobs_{status}"] = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM analysis_jobs{where}{' AND' if where else ' WHERE'} status=?",
                        (*params, status),
                    ).fetchone()[0]
                )
            result["analyses"] = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM news_analyses{where}", params
                ).fetchone()[0]
            )
            if analysis_key:
                result["predictions"] = int(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM predictions p
                        JOIN news_analyses a ON a.id=p.analysis_id
                        WHERE a.analysis_key=?
                        """,
                        (analysis_key,),
                    ).fetchone()[0]
                )
                result["valid_predictions"] = int(conn.execute(
                    """SELECT COUNT(*) FROM predictions p JOIN news_analyses a ON a.id=p.analysis_id
                       WHERE a.analysis_key=? AND p.evaluation_status='valid'""", (analysis_key,)
                ).fetchone()[0])
                result["observe_only_predictions"] = int(conn.execute(
                    """SELECT COUNT(*) FROM predictions p JOIN news_analyses a ON a.id=p.analysis_id
                       WHERE a.analysis_key=? AND p.evaluation_status='observe_only'""", (analysis_key,)
                ).fetchone()[0])
                result["calibration_only_predictions"] = int(conn.execute(
                    """SELECT COUNT(*) FROM predictions p JOIN news_analyses a ON a.id=p.analysis_id
                       WHERE a.analysis_key=? AND p.evaluation_status='calibration_only'""", (analysis_key,)
                ).fetchone()[0])
                result["directional_predictions"] = int(conn.execute(
                    """SELECT COUNT(*) FROM predictions p JOIN news_analyses a ON a.id=p.analysis_id
                       WHERE a.analysis_key=? AND p.valid_for_directional_trading=1""", (analysis_key,)
                ).fetchone()[0])
                result["event_clusters"] = int(conn.execute(
                    """SELECT COUNT(DISTINCT a.event_cluster_id) FROM news_analyses a
                       WHERE a.analysis_key=? AND a.event_cluster_id IS NOT NULL""", (analysis_key,)
                ).fetchone()[0])
                result["attribution_mismatch"] = int(conn.execute(
                    "SELECT COUNT(*) FROM news_analyses WHERE analysis_key=? AND attribution_status='mismatch'",
                    (analysis_key,),
                ).fetchone()[0])
                result["attribution_unresolved"] = int(conn.execute(
                    "SELECT COUNT(*) FROM news_analyses WHERE analysis_key=? AND attribution_status='unresolved'",
                    (analysis_key,),
                ).fetchone()[0])
                for recovery_status in (
                    "recovered", "partial", "hard_failed", "unavailable",
                    "not_needed_integrity_valid", "not_attempted_disabled", "not_attempted_low_impact",
                    "not_attempted_low_signal", "not_attempted_retrospective",
                    "not_attempted_attribution_mismatch",
                    "not_attempted_no_recovery_model",
                ):
                    summary_key = (
                        "integrity_not_needed"
                        if recovery_status == "not_needed_integrity_valid"
                        else f"integrity_{recovery_status}"
                    )
                    result[summary_key] = int(conn.execute(
                        "SELECT COUNT(*) FROM news_analyses WHERE analysis_key=? AND integrity_recovery_status=?",
                        (analysis_key, recovery_status),
                    ).fetchone()[0])
                for economic_status in ("valid", "warning", "failed", "not_applicable", "not_checked"):
                    result[f"economic_{economic_status}"] = int(conn.execute(
                        "SELECT COUNT(*) FROM news_analyses WHERE analysis_key=? AND raw_json LIKE ?",
                        (analysis_key, f'%"economic_plausibility_status":"{economic_status}"%'),
                    ).fetchone()[0])
                for quote_status in ("valid", "warning", "failed", "not_available", "not_available_for_actual_subject"):
                    result[f"target_quote_{quote_status}"] = int(conn.execute(
                        "SELECT COUNT(*) FROM news_analyses WHERE analysis_key=? AND target_quote_integrity_status=?",
                        (analysis_key, quote_status),
                    ).fetchone()[0])
                result["article_scope_market"] = int(conn.execute(
                    "SELECT COUNT(*) FROM news_analyses WHERE analysis_key=? AND article_scope='market'",
                    (analysis_key,),
                ).fetchone()[0])
            else:
                result["predictions"] = int(
                    conn.execute("SELECT COUNT(*) FROM predictions").fetchone()[0]
                )
                result["valid_predictions"] = int(conn.execute("SELECT COUNT(*) FROM predictions WHERE evaluation_status='valid'").fetchone()[0])
                result["observe_only_predictions"] = int(conn.execute("SELECT COUNT(*) FROM predictions WHERE evaluation_status='observe_only'").fetchone()[0])
                result["calibration_only_predictions"] = int(conn.execute("SELECT COUNT(*) FROM predictions WHERE evaluation_status='calibration_only'").fetchone()[0])
                result["directional_predictions"] = int(conn.execute("SELECT COUNT(*) FROM predictions WHERE valid_for_directional_trading=1").fetchone()[0])
                result["event_clusters"] = int(conn.execute("SELECT COUNT(*) FROM event_clusters").fetchone()[0])
                result["attribution_mismatch"] = int(conn.execute(
                    "SELECT COUNT(*) FROM news_analyses WHERE attribution_status='mismatch'"
                ).fetchone()[0])
                result["attribution_unresolved"] = int(conn.execute(
                    "SELECT COUNT(*) FROM news_analyses WHERE attribution_status='unresolved'"
                ).fetchone()[0])
                for recovery_status in (
                    "recovered", "partial", "hard_failed", "unavailable",
                    "not_needed_integrity_valid", "not_attempted_disabled", "not_attempted_low_impact",
                    "not_attempted_low_signal", "not_attempted_retrospective",
                    "not_attempted_attribution_mismatch",
                    "not_attempted_no_recovery_model",
                ):
                    summary_key = (
                        "integrity_not_needed"
                        if recovery_status == "not_needed_integrity_valid"
                        else f"integrity_{recovery_status}"
                    )
                    result[summary_key] = int(conn.execute(
                        "SELECT COUNT(*) FROM news_analyses WHERE integrity_recovery_status=?",
                        (recovery_status,),
                    ).fetchone()[0])
                for economic_status in ("valid", "warning", "failed", "not_applicable", "not_checked"):
                    result[f"economic_{economic_status}"] = int(conn.execute(
                        "SELECT COUNT(*) FROM news_analyses WHERE raw_json LIKE ?",
                        (f'%"economic_plausibility_status":"{economic_status}"%',),
                    ).fetchone()[0])
            for usage_status in ("provisional", "eligible", "observation_only", "excluded"):
                result[f"reaction_targets_{usage_status}"] = int(conn.execute(
                    "SELECT COUNT(*) FROM reaction_targets WHERE usage_status=?",
                    (usage_status,),
                ).fetchone()[0])
            result["reactions_calibration_eligible"] = int(conn.execute(
                "SELECT COUNT(*) FROM market_reactions WHERE calibration_eligible=1"
            ).fetchone()[0])
            result["reactions_observation_eligible"] = int(conn.execute(
                "SELECT COUNT(*) FROM market_reactions WHERE observation_eligible=1"
            ).fetchone()[0])
            return result

    def upsert_financial_statement_fact(self, fact: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "symbol": str(getattr(fact, "symbol") or "").upper(),
            "cik": str(getattr(fact, "cik") or ""),
            "taxonomy": str(getattr(fact, "taxonomy") or ""),
            "tag": str(getattr(fact, "tag") or ""),
            "label": str(getattr(fact, "label") or ""),
            "unit": str(getattr(fact, "unit") or ""),
            "fiscal_year": getattr(fact, "fiscal_year", None),
            "fiscal_period": str(getattr(fact, "fiscal_period") or ""),
            "form": str(getattr(fact, "form") or ""),
            "filed_at": str(getattr(fact, "filed_at") or ""),
            "start_date": getattr(fact, "start_date", None),
            "end_date": str(getattr(fact, "end_date") or ""),
            "frame": str(getattr(fact, "frame") or ""),
            "value": float(getattr(fact, "value")),
            "source_url": str(getattr(fact, "source_url") or ""),
            "accession_number": str(getattr(fact, "accession_number") or ""),
            "source_hash": str(getattr(fact, "source_hash") or ""),
            "ingestion_version": str(getattr(fact, "ingestion_version") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO financial_statement_facts(
                    symbol,cik,taxonomy,tag,label,unit,fiscal_year,fiscal_period,form,
                    filed_at,start_date,end_date,frame,value,source_url,accession_number,
                    source_hash,ingestion_version,created_at,updated_at
                ) VALUES (
                    :symbol,:cik,:taxonomy,:tag,:label,:unit,:fiscal_year,:fiscal_period,:form,
                    :filed_at,:start_date,:end_date,:frame,:value,:source_url,:accession_number,
                    :source_hash,:ingestion_version,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM financial_statement_facts
                   WHERE symbol=? AND taxonomy=? AND tag=? AND unit=? AND
                         COALESCE(fiscal_year,-1)=COALESCE(?,-1) AND fiscal_period=? AND
                         end_date=? AND accession_number=? AND ingestion_version=?""",
                (
                    params["symbol"], params["taxonomy"], params["tag"], params["unit"],
                    params["fiscal_year"], params["fiscal_period"], params["end_date"],
                    params["accession_number"], params["ingestion_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("financial statement fact insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE financial_statement_facts
                       SET cik=?,label=?,form=?,filed_at=?,start_date=?,frame=?,value=?,
                           source_url=?,source_hash=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["cik"], params["label"], params["form"], params["filed_at"],
                        params["start_date"], params["frame"], params["value"],
                        params["source_url"], params["source_hash"], now, int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_financial_statement_facts(
        self,
        limit: int = 50,
        *,
        symbol: str | None = None,
        tag: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if symbol:
            where.append("symbol=?")
            params.append(symbol.strip().upper())
        if tag:
            where.append("tag=?")
            params.append(tag.strip())
        where_sql = "" if not where else "WHERE " + " AND ".join(where)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM financial_statement_facts
                        {where_sql}
                        ORDER BY filed_at DESC,end_date DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def upsert_fred_observation(self, observation: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "series_id": str(getattr(observation, "series_id") or "").upper(),
            "observation_date": str(getattr(observation, "observation_date") or ""),
            "value": float(getattr(observation, "value")),
            "realtime_start": str(getattr(observation, "realtime_start") or ""),
            "realtime_end": str(getattr(observation, "realtime_end") or ""),
            "source_url": str(getattr(observation, "source_url") or ""),
            "source_hash": str(getattr(observation, "source_hash") or ""),
            "ingestion_version": str(getattr(observation, "ingestion_version") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO fred_observations(
                    series_id,observation_date,value,realtime_start,realtime_end,
                    source_url,source_hash,ingestion_version,created_at,updated_at
                ) VALUES (
                    :series_id,:observation_date,:value,:realtime_start,:realtime_end,
                    :source_url,:source_hash,:ingestion_version,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM fred_observations
                   WHERE series_id=? AND observation_date=? AND ingestion_version=?""",
                (params["series_id"], params["observation_date"], params["ingestion_version"]),
            ).fetchone()
            if row is None:
                raise RuntimeError("FRED observation insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE fred_observations
                       SET value=?,realtime_start=?,realtime_end=?,source_url=?,source_hash=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["value"], params["realtime_start"], params["realtime_end"],
                        params["source_url"], params["source_hash"], now, int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_fred_observations(
        self,
        limit: int = 50,
        *,
        series_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if series_id:
            where = "WHERE series_id=?"
            params.append(series_id.strip().upper())
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM fred_observations
                        {where}
                        ORDER BY observation_date DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def upsert_economic_indicator_observation(self, observation: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        raw = getattr(observation, "raw", {})
        params = {
            "canonical_series_id": str(getattr(observation, "canonical_series_id") or "").upper(),
            "provider": str(getattr(observation, "provider") or "").lower(),
            "native_series_id": str(getattr(observation, "native_series_id") or ""),
            "observation_date": str(getattr(observation, "observation_date") or ""),
            "value": float(getattr(observation, "value")),
            "unit": str(getattr(observation, "unit") or ""),
            "frequency": str(getattr(observation, "frequency") or ""),
            "region": str(getattr(observation, "region") or ""),
            "category": str(getattr(observation, "category") or ""),
            "provider_priority": int(getattr(observation, "provider_priority", 50)),
            "realtime_start": str(getattr(observation, "realtime_start") or ""),
            "realtime_end": str(getattr(observation, "realtime_end") or ""),
            "source_url": str(getattr(observation, "source_url") or ""),
            "source_hash": str(getattr(observation, "source_hash") or ""),
            "ingestion_version": str(getattr(observation, "ingestion_version") or ""),
            "raw_json": json.dumps(raw if isinstance(raw, dict) else {}, sort_keys=True, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO economic_indicator_observations(
                    canonical_series_id,provider,native_series_id,observation_date,value,
                    unit,frequency,region,category,provider_priority,
                    realtime_start,realtime_end,source_url,source_hash,ingestion_version,
                    raw_json,created_at,updated_at
                ) VALUES (
                    :canonical_series_id,:provider,:native_series_id,:observation_date,:value,
                    :unit,:frequency,:region,:category,:provider_priority,
                    :realtime_start,:realtime_end,:source_url,:source_hash,:ingestion_version,
                    :raw_json,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM economic_indicator_observations
                   WHERE canonical_series_id=? AND provider=? AND native_series_id=?
                     AND observation_date=? AND ingestion_version=?""",
                (
                    params["canonical_series_id"],
                    params["provider"],
                    params["native_series_id"],
                    params["observation_date"],
                    params["ingestion_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("Economic indicator observation insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE economic_indicator_observations
                       SET value=?,unit=?,frequency=?,region=?,category=?,provider_priority=?,
                           realtime_start=?,realtime_end=?,source_url=?,source_hash=?,raw_json=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["value"], params["unit"], params["frequency"], params["region"],
                        params["category"], params["provider_priority"], params["realtime_start"],
                        params["realtime_end"], params["source_url"], params["source_hash"],
                        params["raw_json"], now, int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_economic_indicator_observations(
        self,
        limit: int = 50,
        *,
        canonical_series_id: str | None = None,
        provider: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if canonical_series_id:
            clauses.append("canonical_series_id=?")
            params.append(canonical_series_id.strip().upper())
        if provider:
            clauses.append("provider=?")
            params.append(provider.strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM economic_indicator_observations
                        {where}
                        ORDER BY observation_date DESC,provider_priority ASC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def upsert_technical_indicator(self, indicator: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "symbol": str(getattr(indicator, "symbol") or "").upper(),
            "as_of_date": str(getattr(indicator, "as_of_date") or ""),
            "price_source": str(getattr(indicator, "price_source") or ""),
            "close": float(getattr(indicator, "close")),
            "return_1d": getattr(indicator, "return_1d", None),
            "return_5d": getattr(indicator, "return_5d", None),
            "return_20d": getattr(indicator, "return_20d", None),
            "sma_20": getattr(indicator, "sma_20", None),
            "sma_50": getattr(indicator, "sma_50", None),
            "ema_12": getattr(indicator, "ema_12", None),
            "ema_26": getattr(indicator, "ema_26", None),
            "macd": getattr(indicator, "macd", None),
            "macd_signal": getattr(indicator, "macd_signal", None),
            "rsi_14": getattr(indicator, "rsi_14", None),
            "volatility_20": getattr(indicator, "volatility_20", None),
            "source_start_date": str(getattr(indicator, "source_start_date") or ""),
            "source_end_date": str(getattr(indicator, "source_end_date") or ""),
            "ingestion_version": str(getattr(indicator, "ingestion_version") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO technical_indicators(
                    symbol,as_of_date,price_source,close,return_1d,return_5d,return_20d,
                    sma_20,sma_50,ema_12,ema_26,macd,macd_signal,rsi_14,volatility_20,
                    source_start_date,source_end_date,ingestion_version,created_at,updated_at
                ) VALUES (
                    :symbol,:as_of_date,:price_source,:close,:return_1d,:return_5d,:return_20d,
                    :sma_20,:sma_50,:ema_12,:ema_26,:macd,:macd_signal,:rsi_14,:volatility_20,
                    :source_start_date,:source_end_date,:ingestion_version,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM technical_indicators
                   WHERE symbol=? AND as_of_date=? AND price_source=? AND ingestion_version=?""",
                (
                    params["symbol"], params["as_of_date"], params["price_source"],
                    params["ingestion_version"],
                ),
            ).fetchone()
            if row is None:
                raise RuntimeError("technical indicator insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE technical_indicators
                       SET close=?,return_1d=?,return_5d=?,return_20d=?,sma_20=?,sma_50=?,
                           ema_12=?,ema_26=?,macd=?,macd_signal=?,rsi_14=?,volatility_20=?,
                           source_start_date=?,source_end_date=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["close"], params["return_1d"], params["return_5d"],
                        params["return_20d"], params["sma_20"], params["sma_50"],
                        params["ema_12"], params["ema_26"], params["macd"],
                        params["macd_signal"], params["rsi_14"], params["volatility_20"],
                        params["source_start_date"], params["source_end_date"], now, int(row["id"]),
                    ),
                )
            return int(row["id"]), created

    def recent_technical_indicators(
        self,
        limit: int = 50,
        *,
        symbol: str | None = None,
        price_source: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if symbol:
            where.append("symbol=?")
            params.append(symbol.strip().upper())
        if price_source:
            where.append("price_source=?")
            params.append(price_source.strip())
        where_sql = "" if not where else "WHERE " + " AND ".join(where)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM technical_indicators
                        {where_sql}
                        ORDER BY as_of_date DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def upsert_external_market_signal(self, signal: Any) -> tuple[int, bool]:
        now = utc_iso(datetime.now(UTC))
        params = {
            "source": str(getattr(signal, "source") or "").lower(),
            "external_id": str(getattr(signal, "external_id") or ""),
            "title": str(getattr(signal, "title") or ""),
            "url": str(getattr(signal, "url") or ""),
            "published_at": str(getattr(signal, "published_at") or ""),
            "region": str(getattr(signal, "region") or "unknown"),
            "theme": str(getattr(signal, "theme") or "unknown"),
            "sentiment_score": getattr(signal, "sentiment_score", None),
            "sentiment_label": str(getattr(signal, "sentiment_label") or "unknown"),
            "confidence": float(getattr(signal, "confidence") or 0.0),
            "assets_json": json.dumps(list(getattr(signal, "assets") or ()), ensure_ascii=False),
            "asset_map_reason": str(getattr(signal, "asset_map_reason") or ""),
            "market_reaction_status": str(getattr(signal, "market_reaction_status") or "pending"),
            "market_reaction_json": json.dumps(getattr(signal, "market_reaction") or {}, ensure_ascii=False, default=str),
            "prediction_gate_status": str(getattr(signal, "prediction_gate_status") or "blocked"),
            "prediction_gate_reason": str(getattr(signal, "prediction_gate_reason") or ""),
            "raw_json": json.dumps(getattr(signal, "raw") or {}, ensure_ascii=False, default=str),
            "ingestion_version": str(getattr(signal, "ingestion_version") or ""),
            "source_news_id": getattr(signal, "source_news_id", None),
            "event_cluster_id": getattr(signal, "event_cluster_id", None),
            "classifier_version": str(getattr(signal, "classifier_version", "") or ""),
            "mapper_version": str(getattr(signal, "mapper_version", "") or ""),
            "config_hash": str(getattr(signal, "config_hash", "") or ""),
            "created_at": now,
            "updated_at": now,
        }
        with self.connect() as conn:
            ensure_news_lineage_v2_schema(conn)
            before = conn.total_changes
            conn.execute(
                """INSERT OR IGNORE INTO external_market_signals(
                    source,external_id,title,url,published_at,region,theme,
                    sentiment_score,sentiment_label,confidence,assets_json,asset_map_reason,
                    market_reaction_status,market_reaction_json,prediction_gate_status,
                    prediction_gate_reason,raw_json,ingestion_version,source_news_id,event_cluster_id,
                    classifier_version,mapper_version,config_hash,created_at,updated_at
                ) VALUES (
                    :source,:external_id,:title,:url,:published_at,:region,:theme,
                    :sentiment_score,:sentiment_label,:confidence,:assets_json,:asset_map_reason,
                    :market_reaction_status,:market_reaction_json,:prediction_gate_status,
                    :prediction_gate_reason,:raw_json,:ingestion_version,:source_news_id,:event_cluster_id,
                    :classifier_version,:mapper_version,:config_hash,:created_at,:updated_at
                )""",
                params,
            )
            row = conn.execute(
                """SELECT id FROM external_market_signals
                   WHERE source=? AND external_id=? AND ingestion_version=?""",
                (params["source"], params["external_id"], params["ingestion_version"]),
            ).fetchone()
            if row is None:
                raise RuntimeError("external market signal insert did not return a row")
            created = conn.total_changes > before
            if not created:
                conn.execute(
                    """UPDATE external_market_signals
                       SET title=?,url=?,published_at=?,region=?,theme=?,sentiment_score=?,
                           sentiment_label=?,confidence=?,assets_json=?,asset_map_reason=?,
                           market_reaction_status=?,market_reaction_json=?,
                           prediction_gate_status=?,prediction_gate_reason=?,raw_json=?,
                           source_news_id=COALESCE(?,source_news_id),
                           event_cluster_id=COALESCE(?,event_cluster_id),classifier_version=?,
                           mapper_version=?,config_hash=?,updated_at=?
                       WHERE id=?""",
                    (
                        params["title"], params["url"], params["published_at"], params["region"],
                        params["theme"], params["sentiment_score"], params["sentiment_label"],
                        params["confidence"], params["assets_json"], params["asset_map_reason"],
                        params["market_reaction_status"], params["market_reaction_json"],
                        params["prediction_gate_status"], params["prediction_gate_reason"],
                        params["raw_json"], params["source_news_id"], params["event_cluster_id"],
                        params["classifier_version"], params["mapper_version"], params["config_hash"],
                        now, int(row["id"]),
                    ),
                )
            if params["source_news_id"] is not None:
                cluster_id = assign_article_to_event_cluster(
                    conn,
                    int(params["source_news_id"]),
                )
                conn.execute(
                    """UPDATE external_market_signals
                       SET event_cluster_id=COALESCE(event_cluster_id, ?)
                       WHERE id=?""",
                    (cluster_id, int(row["id"])),
                )
                link_external_signal_to_source_news(
                    conn,
                    signal_id=int(row["id"]),
                    news_id=int(params["source_news_id"]),
                    link_type="primary_source",
                    confidence=1.0,
                )
                recompute_event_cluster_roles(conn, cluster_id)
                if params["source"] == "marketaux":
                    upsert_raw_news_processing_status(
                        conn,
                        news_id=int(params["source_news_id"]),
                        source_system="marketaux",
                        provider="marketaux",
                        processing_status="derived_signal_created",
                        skip_reason=None,
                        skip_flags={"source": "external_market_signal_upsert"},
                        derived_signal_id=int(row["id"]),
                        event_cluster_id=cluster_id,
                        classifier_version=params["classifier_version"],
                        mapper_version=params["mapper_version"],
                        config_hash=params["config_hash"],
                    )
            return int(row["id"]), created

    def recent_external_market_signals(
        self,
        limit: int = 50,
        *,
        source: str | None = None,
        region: str | None = None,
        theme: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[Any] = []
        if source:
            where.append("source=?")
            params.append(source.strip().lower())
        if region:
            where.append("region=?")
            params.append(region.strip())
        if theme:
            where.append("theme=?")
            params.append(theme.strip())
        where_sql = "" if not where else "WHERE " + " AND ".join(where)
        params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM external_market_signals
                        {where_sql}
                        ORDER BY published_at DESC,id DESC
                        LIMIT ?""",
                    tuple(params),
                ).fetchall()
            ]

    def recent_analyses(self, limit: int = 20, analysis_key: str | None = None):
        sql = """
            SELECT a.id,a.news_id,a.completed_at,a.provider,a.model_name,
                   a.prompt_version,a.primary_symbol,a.event_scope,a.subject_entity,a.event_subject_symbol,
                   a.subject_asset_type,a.target_asset_type,a.target_role,a.target_assets_json,
                   a.attribution_status,a.attribution_confidence,a.original_subscription_symbol,
                   a.subject_target_available,a.attribution_reason,a.article_scope,a.article_primary_subject,
                   a.mentioned_symbols_json,a.subscription_entity_role,a.target_quote_integrity_status,
                   a.target_quote_integrity_reasons_json,a.delivery_reference_symbol,a.delivery_reference_price,
                   a.delivery_reference_price_source,a.event_type,a.event_subtype,
                   a.sentiment,a.prediction_eligible,a.reaction_observation_eligible,a.analysis_priority,
                   a.impact_score,a.confidence,a.source_tier,a.official_source_status,a.event_cluster_id,a.summary,a.raw_json,
                   a.text_integrity_status,a.validated_fact_count,a.rejected_fact_count,a.fact_validation_notes_json,
                   a.integrity_initial_status,a.integrity_recovery_status,a.integrity_recovery_model,a.integrity_recovery_attempts,n.headline,
                   (SELECT COUNT(*) FROM predictions p WHERE p.analysis_id=a.id) AS prediction_count
            FROM news_analyses a
            JOIN news_articles n ON n.id=a.news_id
        """
        params: list[Any] = []
        if analysis_key:
            sql += " WHERE a.analysis_key=?"
            params.append(analysis_key)
        sql += " ORDER BY a.completed_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def prediction_rows(self, analysis_id: int):
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT symbol,horizon,expected_return,bear_return,bull_return,
                       up_probability,flat_probability,down_probability,
                       confidence,evaluation_due_at,is_valid,invalid_reason,
                       evaluation_status,quality_reason,evaluation_label,
                       valid_for_directional_trading,valid_for_probability_calibration
                FROM predictions WHERE analysis_id=?
                ORDER BY CASE horizon
                    WHEN '15m' THEN 1 WHEN '1h' THEN 2 WHEN 'close' THEN 3
                    WHEN '1d' THEN 4 WHEN '3d' THEN 5 ELSE 99 END
                """,
                (analysis_id,),
            ).fetchall()

    def assign_event_cluster(
        self,
        *,
        analysis_id: int,
        news_id: int,
        analysis: NewsAnalysis,
        headline: str,
        body: str,
        published_at: str,
        similarity_threshold: float = 84.0,
        window_hours: int = 48,
    ) -> int:
        identity = build_cluster_identity(analysis, headline=headline, body=body, published_at=published_at)
        now = utc_iso(datetime.now(UTC))
        event_time = identity.event_time
        cutoff = utc_iso(event_time - timedelta(hours=window_hours))
        ceiling = utc_iso(event_time + timedelta(hours=window_hours))
        has_official = analysis.official_source_status == "attached"
        rank = source_rank(analysis.source_tier, analysis.event_stage, has_official, len(analysis.key_facts))
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            candidates = [dict(row) for row in conn.execute(
                """SELECT * FROM event_clusters WHERE event_scope=? AND event_type=?
                   AND cluster_family=? AND subject_key=?
                   AND event_time BETWEEN ? AND ? ORDER BY event_time""",
                (identity.event_scope, identity.event_type, identity.cluster_family, identity.subject_key, cutoff, ceiling),
            ).fetchall()]
            best = None
            best_similarity = 0.0
            for candidate in candidates:
                similarity = cluster_similarity(identity, candidate)
                if similarity > best_similarity:
                    best, best_similarity = candidate, similarity
            if best is None or best_similarity < similarity_threshold:
                cluster_key = identity.cluster_key
                collision = conn.execute("SELECT id FROM event_clusters WHERE cluster_key=?", (cluster_key,)).fetchone()
                if collision is not None:
                    cluster_key = f"{cluster_key}:{analysis_id}"
                cursor = conn.execute(
                    """INSERT INTO event_clusters(cluster_key,event_scope,event_type,cluster_family,subject_key,primary_symbol,
                       subject_symbol,target_symbol,event_anchor,event_time,comparison_text,representative_analysis_id,representative_news_id,
                       representative_rank,source_tier,member_count,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (cluster_key, identity.event_scope, identity.event_type, identity.cluster_family, identity.subject_key,
                     identity.primary_symbol, identity.subject_symbol, identity.target_symbol, identity.event_anchor,
                     utc_iso(event_time), identity.comparison_text, analysis_id, news_id, rank, analysis.source_tier, now, now),
                )
                cluster_id = int(cursor.lastrowid)
                best_similarity = 100.0
            else:
                cluster_id = int(best["id"])
                conn.execute("UPDATE event_clusters SET member_count=member_count+1,updated_at=? WHERE id=?", (now, cluster_id))
                if rank > int(best.get("representative_rank") or 0):
                    conn.execute(
                        """UPDATE event_clusters SET representative_analysis_id=?,representative_news_id=?,
                           representative_rank=?,source_tier=?,comparison_text=?,updated_at=? WHERE id=?""",
                        (analysis_id, news_id, rank, analysis.source_tier, identity.comparison_text, now, cluster_id),
                    )
            role = cluster_member_role(analysis)
            conn.execute(
                """INSERT OR IGNORE INTO event_cluster_members(cluster_id,analysis_id,news_id,member_role,similarity,source_rank,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (cluster_id, analysis_id, news_id, role, best_similarity, rank, now),
            )
            conn.execute("UPDATE news_analyses SET event_cluster_id=? WHERE id=?", (cluster_id, analysis_id))
            conn.execute("UPDATE official_documents SET event_cluster_id=? WHERE news_id=?", (cluster_id, news_id))
            conn.execute("COMMIT")
            return cluster_id

    def recent_clusters(self, limit: int = 20):
        with self.connect() as conn:
            return conn.execute(
                """SELECT c.*,n.headline,
                          (SELECT group_concat(m.member_role, ',')
                           FROM event_cluster_members m WHERE m.cluster_id=c.id) AS member_roles
                   FROM event_clusters c
                   LEFT JOIN news_articles n ON n.id=c.representative_news_id
                   ORDER BY c.updated_at DESC LIMIT ?""", (limit,)
            ).fetchall()

    def official_source_summary(self) -> dict[str, int]:
        with self.connect() as conn:
            return {str(row["status"]): int(row["count"]) for row in conn.execute(
                "SELECT status,COUNT(*) AS count FROM official_documents GROUP BY status"
            ).fetchall()}

    def job_events(self, job_id: int) -> Iterable[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM analysis_job_events WHERE job_id=? ORDER BY id",
                (job_id,),
            ).fetchall()
