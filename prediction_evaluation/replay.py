from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from recorder.analysis_models import NewsAnalysis, validate_primary_horizons
from recorder.analysis_service import AIAnalysisService
from recorder.database import Database
from recorder.economic_plausibility import EconomicPlausibilityConfig, assess_economic_plausibility
from recorder.prediction_quality import evaluate_prediction_quality
from recorder.prompting import prompt_hash, stable_json
from recorder.text_integrity import validate_analysis_text

from .config import EvaluationConfig
from .db import EvaluationDatabase, utc_iso

UTC = timezone.utc
FORBIDDEN_SNAPSHOT_KEYS = {
    "realized_return",
    "future_return",
    "reaction_price",
    "post_event_price",
    "market_reactions",
    "prediction_outcome",
    "actual_direction",
}
PRICE_REACTION_PATTERN = re.compile(
    r"\b(shares?|stock|etf|index)\s+(rose|fell|jumped|surged|plunged|slid|gained|lost)\s+\d",
    re.IGNORECASE,
)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _walk_forbidden(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_SNAPSHOT_KEYS:
                findings.append(f"forbidden snapshot key {path}.{key}")
            findings.extend(_walk_forbidden(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_walk_forbidden(child, f"{path}[{index}]"))
    return findings


def _max_timestamp(values: Iterable[str | None]) -> datetime | None:
    parsed = [item for item in (_parse_time(value) for value in values) if item is not None]
    return max(parsed) if parsed else None


@dataclass(frozen=True)
class ReplayRunResult:
    planned: int
    processed: int
    completed: int
    failed: int
    rejected_for_leakage: int

    def as_dict(self) -> dict[str, int]:
        return {
            "planned": self.planned,
            "processed": self.processed,
            "completed": self.completed,
            "failed": self.failed,
            "rejected_for_leakage": self.rejected_for_leakage,
        }


class HistoricalReplayEngine:
    def __init__(self, settings: Any, config: EvaluationConfig):
        self.settings = settings
        self.config = config
        # Replay commands must upgrade recorder-owned provenance columns before
        # inspecting an older local database.
        Database(config.database_path)
        self.db = EvaluationDatabase(config.database_path)
        self.db.migrate()

    def _candidate_rows(
        self,
        *,
        start: datetime,
        end: datetime,
        event_types: Sequence[str],
        limit: int | None,
    ) -> list[dict[str, Any]]:
        normalized = [event_type.lower() for event_type in event_types]
        placeholders = ",".join("?" for _ in normalized)
        limit_sql = " LIMIT ?" if limit is not None else ""
        params: list[Any] = [utc_iso(start), utc_iso(end), *normalized]
        if limit is not None:
            params.append(limit)
        sql = f"""
            SELECT a.id AS source_analysis_id,a.news_id,a.event_cluster_id,
                   a.primary_symbol,a.event_type,a.event_subtype,a.model_name,
                    a.prompt_version,a.attribution_status,a.raw_json,a.started_at,
                    a.completed_at,n.provider,n.headline,n.body,n.published_at,
                    n.received_at,n.provider_published_at,n.source_accepted_at,
                    n.first_seen_at,n.content_updated_at,n.timestamp_quality,
                    n.timestamp_source,
                    COALESCE(n.source_accepted_at,n.provider_published_at,
                             n.first_seen_at,n.received_at) AS source_available_at,
                    pi.input_hash,pi.input_snapshot_json,
                    pi.baseline_snapshot_json,pi.created_at AS snapshot_created_at
            FROM news_analyses a
            JOIN news_articles n ON n.id=a.news_id
            JOIN prediction_inputs pi ON pi.analysis_id=a.id
            WHERE COALESCE(n.source_accepted_at,n.provider_published_at,
                           n.first_seen_at,n.received_at)>=?
              AND COALESCE(n.source_accepted_at,n.provider_published_at,
                           n.first_seen_at,n.received_at)<?
              AND LOWER(a.event_type) IN ({placeholders})
            ORDER BY source_available_at,a.id
            {limit_sql}
        """
        with self.db.connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    @staticmethod
    def _prediction_cutoff(row: Mapping[str, Any], snapshot: Mapping[str, Any]) -> datetime:
        candidates = [row.get("source_available_at")]
        for key in ("baseline_market_snapshot", "delivery_market_snapshot", "target_quote_audit_snapshot"):
            for quote in snapshot.get(key, []) or []:
                if isinstance(quote, dict):
                    candidates.append(quote.get("captured_at"))
        cutoff = _max_timestamp(str(value) if value is not None else None for value in candidates)
        if cutoff is None:
            raise ValueError("Replay candidate has no usable point-in-time timestamp")
        return cutoff

    def plan(
        self,
        *,
        start: datetime,
        end: datetime,
        event_types: Sequence[str] = ("earnings", "guidance"),
        limit: int | None = None,
    ) -> int:
        rows = self._candidate_rows(start=start, end=end, event_types=event_types, limit=limit)
        inserted = 0
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for row in rows:
                try:
                    snapshot = json.loads(str(row["input_snapshot_json"]))
                    if not isinstance(snapshot, dict):
                        continue
                    cutoff = self._prediction_cutoff(row, snapshot)
                    snapshot_json = stable_json(snapshot)
                    evidence_material = stable_json(
                        {
                            "news_id": row["news_id"],
                            "headline": row["headline"],
                            "body": snapshot.get("article_body", ""),
                            "published_at": row["published_at"],
                            "received_at": row["received_at"],
                            "timestamp_provenance": {
                                "available_at": row["source_available_at"],
                                "timestamp_quality": row["timestamp_quality"],
                                "timestamp_source": row["timestamp_source"],
                                "content_updated_at": row["content_updated_at"],
                            },
                            "baselines": snapshot.get("baseline_market_snapshot", []),
                            "official_evidence": snapshot.get("official_evidence", []),
                        }
                    )
                    job_uuid = f"replay-{int(row['source_analysis_id'])}-{uuid.uuid4().hex[:10]}"
                    now = utc_iso()
                    cursor = conn.execute(
                        """
                        INSERT OR IGNORE INTO replay_jobs(
                            job_uuid,source_analysis_id,news_id,event_cluster_id,symbol,
                            event_type,replay_timestamp,prediction_cutoff_at,
                            source_available_at,timestamp_quality,timestamp_source,content_updated_at,status,
                            provider_name,model_name,prompt_version,policy_version,input_hash,
                            evidence_hash,snapshot_json,created_at,updated_at
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            job_uuid,row["source_analysis_id"],row["news_id"],row["event_cluster_id"],
                            str(row["primary_symbol"]).upper(),str(row["event_type"]).lower(),
                            utc_iso(cutoff),utc_iso(cutoff),str(row["source_available_at"] or ""),
                            str(row["timestamp_quality"] or "receipt_only"),
                            str(row["timestamp_source"] or "received_at"),
                            row["content_updated_at"],"queued",str(row["provider"]),
                            str(row["model_name"]),str(self.settings.ai_prompt_version),
                            self.config.policy_version,str(row["input_hash"]),_sha(evidence_material),
                            snapshot_json,now,now,
                        ),
                    )
                    if cursor.rowcount:
                        inserted += 1
                        job_id = int(cursor.lastrowid)
                        article_hash = _sha(
                            str(snapshot.get("headline") or row["headline"])
                            + "\n"
                            + str(snapshot.get("article_body") or "")
                        )
                        conn.execute(
                            """
                            INSERT INTO replay_evidence(
                                job_id,source_type,source_id,published_at,available_at,used_at,
                                allowed_at_replay_time,content_hash,metadata_json
                            ) VALUES (?,?,?,?,?,?,?,?,?)
                            """,
                            (
                                job_id,"news_article",str(row["news_id"]),row["provider_published_at"],
                                row["source_available_at"],utc_iso(cutoff),1,article_hash,
                                json.dumps(
                                    {
                                        "provider": row["provider"],
                                        "timestamp_quality": row["timestamp_quality"],
                                        "timestamp_source": row["timestamp_source"],
                                    },
                                    separators=(",", ":"),
                                ),
                            ),
                        )
                        for quote in snapshot.get("baseline_market_snapshot", []) or []:
                            if not isinstance(quote, dict):
                                continue
                            available = str(quote.get("captured_at") or "")
                            allowed = int((_parse_time(available) or cutoff) <= cutoff)
                            conn.execute(
                                """
                                INSERT OR IGNORE INTO replay_evidence(
                                    job_id,source_type,source_id,available_at,used_at,
                                    allowed_at_replay_time,exclusion_reason,content_hash,metadata_json
                                ) VALUES (?,?,?,?,?,?,?,?,?)
                                """,
                                (
                                    job_id,"market_baseline",str(quote.get("symbol") or "unknown"),
                                    available,utc_iso(cutoff),allowed,
                                    "" if allowed else "quote captured after replay cutoff",
                                    _sha(stable_json(quote)),stable_json(quote),
                                ),
                            )
                except (TypeError, ValueError, json.JSONDecodeError):
                    continue
            conn.execute("COMMIT")
        return inserted

    def audit_job(self, job_id: int, *, strict: bool = True) -> tuple[str, list[str]]:
        with self.db.connect() as conn:
            row = conn.execute(
                """
                SELECT j.*,a.raw_json AS source_raw_json,a.attribution_status,
                       n.headline
                FROM replay_jobs j
                JOIN news_analyses a ON a.id=j.source_analysis_id
                JOIN news_articles n ON n.id=j.news_id
                WHERE j.id=?
                """,
                (job_id,),
            ).fetchone()
            evidence = [dict(item) for item in conn.execute(
                "SELECT * FROM replay_evidence WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()]
        if row is None:
            return "FAIL", ["replay job not found"]
        data = dict(row)
        reasons: list[str] = []
        warnings: list[str] = []
        cutoff = _parse_time(str(data["prediction_cutoff_at"]))
        source_available = _parse_time(str(data.get("source_available_at") or ""))
        content_updated = _parse_time(str(data.get("content_updated_at") or ""))
        if cutoff is None:
            reasons.append("invalid replay cutoff")
        else:
            if source_available is None:
                reasons.append("missing source availability timestamp")
            elif source_available > cutoff:
                reasons.append("source became available after replay cutoff")
            if content_updated is not None and content_updated > cutoff:
                reasons.append("article content was updated after replay cutoff")
        timestamp_quality = str(data.get("timestamp_quality") or "receipt_only").lower()
        if timestamp_quality == "receipt_only":
            warnings.append("timestamp quality is receipt_only; strict replay requires review")
        elif timestamp_quality not in {"source_accepted", "provider_published", "first_seen"}:
            reasons.append(f"unknown timestamp quality: {timestamp_quality}")
        if str(data.get("attribution_status") or "").lower() == "mismatch":
            reasons.append("attribution mismatch")
        source_raw = json.loads(str(data.get("source_raw_json") or "{}"))
        if str(source_raw.get("event_stage") or "").lower() == "retrospective":
            reasons.append("retrospective source analysis")
        if str(data.get("event_type") or "").lower() == "market_price_update":
            reasons.append("descriptive market-price update")
        snapshot = json.loads(str(data["snapshot_json"]))
        reasons.extend(_walk_forbidden(snapshot))
        if not snapshot.get("anti_leakage_note"):
            warnings.append("snapshot has no explicit anti-leakage note")
        for item in evidence:
            if int(item["allowed_at_replay_time"]) != 1:
                reasons.append(
                    f"evidence unavailable at replay time: {item['source_type']}:{item['source_id']}"
                )
            available = _parse_time(str(item.get("available_at") or ""))
            if cutoff is not None and available is not None and available > cutoff:
                reasons.append(
                    f"evidence timestamp after cutoff: {item['source_type']}:{item['source_id']}"
                )
        body = str(snapshot.get("article_body") or "")
        stage = str(source_raw.get("event_stage") or "unknown").lower()
        if PRICE_REACTION_PATTERN.search(body) and stage not in {"initial_release", "press_conference"}:
            warnings.append("article text may describe a contemporaneous price reaction")
        status = "FAIL" if reasons else ("REVIEW_REQUIRED" if warnings and strict else "PASS")
        all_reasons = reasons + warnings
        with self.db.connect() as conn:
            conn.execute(
                """
                UPDATE replay_jobs SET leakage_check_status=?,leakage_reasons_json=?,updated_at=?
                WHERE id=?
                """,
                (status,json.dumps(all_reasons, ensure_ascii=False),utc_iso(),job_id),
            )
        return status, all_reasons

    def audit(self, *, strict: bool = True) -> dict[str, Any]:
        with self.db.connect() as conn:
            job_ids = [int(row["id"]) for row in conn.execute("SELECT id FROM replay_jobs ORDER BY id").fetchall()]
        counts = {"PASS": 0, "FAIL": 0, "REVIEW_REQUIRED": 0}
        findings: list[dict[str, Any]] = []
        for job_id in job_ids:
            status, reasons = self.audit_job(job_id, strict=strict)
            counts[status] += 1
            if reasons:
                findings.append({"job_id": job_id, "status": status, "reasons": reasons})
        return {"strict": strict, "job_count": len(job_ids), "status_counts": counts, "findings": findings}

    def _quality_config(self) -> dict[str, Any]:
        return {
            "min_analysis_confidence": self.settings.prediction_min_analysis_confidence,
            "min_prediction_confidence": self.settings.prediction_min_confidence,
            "min_top_probability": self.settings.prediction_min_top_probability,
            "min_probability_edge": self.settings.prediction_min_probability_edge,
            "max_range_width_by_horizon": {
                "15m": self.settings.prediction_max_range_15m,
                "1h": self.settings.prediction_max_range_1h,
                "close": self.settings.prediction_max_range_close,
                "1d": self.settings.prediction_max_range_1d,
                "3d": self.settings.prediction_max_range_3d,
            },
            "official_required_event_types": self.settings.prediction_official_required_event_types,
            "min_validated_facts_without_official": self.settings.prediction_min_validated_facts_without_official,
            "integrity_partial_calibration_only": self.settings.ai_integrity_recovery_partial_calibration_only,
        }

    def _analyze_snapshot(self, service: AIAnalysisService, snapshot: dict[str, Any]):
        result, actual_provider, actual_model = service._analyze(snapshot)  # noqa: SLF001
        service._apply_quality_guards(result.analysis, snapshot)  # noqa: SLF001
        raw_fact_count = len(result.analysis.key_facts)
        raw_high_count = sum(1 for fact in result.analysis.key_facts if fact.importance in {"high", "critical"})
        initial_integrity = validate_analysis_text(result.analysis, snapshot, suppress_on_failure=False)
        result, _ = service._recover_integrity_if_needed(  # noqa: SLF001
            result=result,
            snapshot=snapshot,
            initial_integrity=initial_integrity,
            raw_fact_count=raw_fact_count,
            raw_high_importance_count=raw_high_count,
        )
        assess_economic_plausibility(
            result.analysis,
            snapshot,
            EconomicPlausibilityConfig(
                enabled=self.settings.economic_plausibility_enabled,
                target_max_price_multiple=self.settings.economic_target_max_price_multiple,
                target_min_price_multiple=self.settings.economic_target_min_price_multiple,
                target_max_change_ratio=self.settings.economic_target_max_change_ratio,
                fundamental_max_actual_expected_ratio=self.settings.economic_fundamental_max_actual_expected_ratio,
                fundamental_max_actual_prior_ratio=self.settings.economic_fundamental_max_actual_prior_ratio,
            ),
        )
        analysis = validate_primary_horizons(
            result.analysis,
            expected_symbol=str(snapshot["attributed_primary_symbol"]),
            required_horizons=self.settings.ai_horizons,
        )
        return result, analysis, result.provider_name or actual_provider, result.model_name or actual_model

    def _evaluation_due_at(self, conn: sqlite3.Connection, news_id: int, symbol: str, horizon: str) -> str | None:
        row = conn.execute(
            """
            SELECT due_at FROM reaction_targets
            WHERE news_id=? AND UPPER(symbol)=UPPER(?) AND UPPER(horizon)=UPPER(?)
            """,
            (news_id, symbol, horizon),
        ).fetchone()
        return str(row["due_at"]) if row else None

    def _execute_job(self, service: AIAnalysisService, job: Mapping[str, Any]) -> None:
        job_id = int(job["id"])
        started = datetime.now(UTC)
        snapshot = json.loads(str(job["snapshot_json"]))
        result, analysis, provider, model = self._analyze_snapshot(service, snapshot)
        completed = datetime.now(UTC)
        cutoff = _parse_time(str(job["prediction_cutoff_at"]))
        if cutoff is None:
            raise ValueError("Invalid replay prediction cutoff")
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT OR REPLACE INTO replay_analyses(
                    job_id,provider_name,model_name,prompt_version,response_id,
                    started_at,completed_at,duration_ms,input_tokens,output_tokens,
                    event_type,event_stage,prediction_eligible,attribution_status,
                    source_tier,confidence,raw_json,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    job_id,provider,model,str(job["prompt_version"]),result.response_id,
                    utc_iso(started),utc_iso(completed),result.duration_ms,result.input_tokens,
                    result.output_tokens,analysis.event_type,analysis.event_stage,
                    int(analysis.prediction_eligible),analysis.attribution_status,
                    analysis.source_tier,analysis.confidence,analysis.model_dump_json(),utc_iso(),
                ),
            )
            replay_analysis_id = int(cursor.lastrowid)
            # INSERT OR REPLACE may change the row id. Resolve it deterministically.
            replay_analysis_id = int(conn.execute(
                "SELECT id FROM replay_analyses WHERE job_id=?", (job_id,)
            ).fetchone()["id"])
            conn.execute("DELETE FROM replay_predictions WHERE replay_analysis_id=?", (replay_analysis_id,))
            quality_config = self._quality_config()
            for prediction in analysis.predictions:
                due_at = self._evaluation_due_at(
                    conn, int(job["news_id"]), prediction.symbol, prediction.horizon
                )
                if not analysis.prediction_eligible:
                    status, reason, directional, calibration = (
                        "invalid","analysis is not prediction eligible",False,False
                    )
                elif due_at is None:
                    status, reason, directional, calibration = (
                        "invalid","matching reaction target unavailable",False,False
                    )
                elif (_parse_time(due_at) or cutoff) <= cutoff:
                    status, reason, directional, calibration = (
                        "invalid","replay prediction cutoff is after evaluation horizon",False,False
                    )
                else:
                    decision = evaluate_prediction_quality(
                        analysis,prediction,
                        min_analysis_confidence=quality_config["min_analysis_confidence"],
                        min_prediction_confidence=quality_config["min_prediction_confidence"],
                        min_top_probability=quality_config["min_top_probability"],
                        min_probability_edge=quality_config["min_probability_edge"],
                        max_range_width_by_horizon=quality_config["max_range_width_by_horizon"],
                        official_required_event_types=tuple(quality_config["official_required_event_types"]),
                        min_validated_facts_without_official=quality_config["min_validated_facts_without_official"],
                        integrity_partial_calibration_only=quality_config["integrity_partial_calibration_only"],
                    )
                    status, reason = decision.status, decision.reason
                    directional = decision.valid_for_directional_trading
                    calibration = decision.valid_for_probability_calibration
                is_valid = int(status in {"valid", "calibration_only"} and calibration)
                conn.execute(
                    """
                    INSERT INTO replay_predictions(
                        replay_analysis_id,job_id,news_id,symbol,horizon,expected_return,
                        bear_return,bull_return,up_probability,flat_probability,
                        down_probability,confidence,prediction_as_of,evaluation_due_at,
                        evaluation_status,quality_reason,is_valid,
                        valid_for_directional_trading,valid_for_probability_calibration,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        replay_analysis_id,job_id,job["news_id"],prediction.symbol,
                        prediction.horizon.upper(),prediction.expected_return,prediction.bear_return,
                        prediction.bull_return,prediction.up_probability,prediction.flat_probability,
                        prediction.down_probability,prediction.confidence,utc_iso(cutoff),due_at,
                        status,reason,is_valid,int(directional),int(calibration),utc_iso(),
                    ),
                )
            conn.execute(
                """
                UPDATE replay_jobs SET status='complete',attempts=attempts+1,
                    provider_name=?,model_name=?,started_at=?,completed_at=?,error_message=NULL,
                    updated_at=? WHERE id=?
                """,
                (provider,model,utc_iso(started),utc_iso(completed),utc_iso(),job_id),
            )
            conn.execute("COMMIT")

    def run(
        self,
        *,
        start: datetime,
        end: datetime,
        event_types: Sequence[str] = ("earnings", "guidance"),
        limit: int | None = None,
        strict: bool = True,
        plan_only: bool = False,
    ) -> ReplayRunResult:
        planned = self.plan(start=start, end=end, event_types=event_types, limit=limit)
        with self.db.connect() as conn:
            jobs = [dict(row) for row in conn.execute(
                """
                SELECT * FROM replay_jobs
                WHERE replay_timestamp>=? AND replay_timestamp<? AND status IN ('queued','failed')
                ORDER BY replay_timestamp,id
                """,
                (utc_iso(start),utc_iso(end)),
            ).fetchall()]
        if limit is not None:
            jobs = jobs[:limit]
        if plan_only:
            for job in jobs:
                self.audit_job(int(job["id"]), strict=strict)
            return ReplayRunResult(planned, 0, 0, 0, 0)

        service = AIAnalysisService(self.settings)
        processed = completed = failed = rejected = 0
        for job in jobs:
            status, reasons = self.audit_job(int(job["id"]), strict=strict)
            if status != "PASS":
                rejected += 1
                with self.db.connect() as conn:
                    conn.execute(
                        "UPDATE replay_jobs SET status='rejected',error_message=?,updated_at=? WHERE id=?",
                        ("; ".join(reasons)[:2000],utc_iso(),job["id"]),
                    )
                continue
            processed += 1
            try:
                self._execute_job(service, job)
                completed += 1
            except Exception as exc:
                failed += 1
                with self.db.connect() as conn:
                    conn.execute(
                        """
                        UPDATE replay_jobs SET status='failed',attempts=attempts+1,
                            error_message=?,completed_at=?,updated_at=? WHERE id=?
                        """,
                        (f"{exc.__class__.__name__}: {exc}"[:2000],utc_iso(),utc_iso(),job["id"]),
                    )
        return ReplayRunResult(planned, processed, completed, failed, rejected)

    def report(self) -> dict[str, Any]:
        with self.db.connect() as conn:
            status_rows = conn.execute(
                "SELECT status,COUNT(*) n FROM replay_jobs GROUP BY status ORDER BY status"
            ).fetchall()
            leakage_rows = conn.execute(
                "SELECT leakage_check_status,COUNT(*) n FROM replay_jobs GROUP BY leakage_check_status ORDER BY leakage_check_status"
            ).fetchall()
            predictions = conn.execute("SELECT COUNT(*) n FROM replay_predictions").fetchone()["n"]
            recent = [dict(row) for row in conn.execute(
                """
                SELECT id,news_id,event_type,symbol,replay_timestamp,status,
                       leakage_check_status,model_name,error_message
                FROM replay_jobs ORDER BY id DESC LIMIT 20
                """
            ).fetchall()]
        return {
            "jobs_by_status": {str(row["status"]): int(row["n"]) for row in status_rows},
            "jobs_by_leakage_status": {str(row["leakage_check_status"]): int(row["n"]) for row in leakage_rows},
            "replay_predictions": int(predictions),
            "recent_jobs": recent,
        }
