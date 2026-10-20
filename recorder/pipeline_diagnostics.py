from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .database import Database

UTC = timezone.utc
DIAGNOSTIC_POLICY_VERSION = "prediction-gate-diagnostic-v1"

GATE_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS prediction_gate_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL,
    news_id INTEGER NOT NULL,
    gate_order INTEGER NOT NULL,
    gate_name TEXT NOT NULL,
    passed INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason_detail TEXT NOT NULL DEFAULT '',
    is_primary_failure INTEGER NOT NULL DEFAULT 0,
    audit_source TEXT NOT NULL DEFAULT 'derived',
    policy_version TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    FOREIGN KEY(analysis_id) REFERENCES news_analyses(id) ON DELETE CASCADE,
    FOREIGN KEY(news_id) REFERENCES news_articles(id),
    UNIQUE(analysis_id, gate_name, policy_version)
);
CREATE INDEX IF NOT EXISTS idx_prediction_gate_audit_failure
    ON prediction_gate_audit(policy_version, passed, is_primary_failure, reason_code);
CREATE INDEX IF NOT EXISTS idx_prediction_gate_audit_analysis
    ON prediction_gate_audit(analysis_id, gate_order);
"""


def utc_iso(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _get(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        return source.get(key, default)
    try:
        return source[key]
    except (KeyError, IndexError, TypeError):
        return getattr(source, key, default)


def _parse_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


@dataclass(frozen=True)
class GateDecision:
    gate_order: int
    gate_name: str
    passed: bool
    reason_code: str
    reason_detail: str = ""
    is_primary_failure: bool = False


def derive_gate_decisions(
    analysis: Any,
    *,
    raw: Mapping[str, Any] | None = None,
    prediction_count: int | None = None,
    official_required_event_types: Sequence[str] = (),
) -> list[GateDecision]:
    """Derive an auditable explanation from the final immutable analysis state.

    These diagnostics do not change prediction eligibility. They explain the
    already-finalized recorder decision and make zero-prediction pipelines
    observable without relaxing any safety gate.
    """

    raw = dict(raw or {})
    event_type = str(_get(analysis, "event_type", raw.get("event_type", "unknown"))).lower()
    event_stage = str(raw.get("event_stage") or _get(analysis, "event_stage", "unknown")).lower()
    causal_signal = _as_bool(_get(analysis, "causal_signal", raw.get("causal_signal", False)))
    prediction_eligible = _as_bool(
        _get(analysis, "prediction_eligible", raw.get("prediction_eligible", False))
    )
    attribution_status = str(_get(analysis, "attribution_status", "unresolved")).lower()
    subject_target_available = _as_bool(_get(analysis, "subject_target_available", True))
    quote_status = str(_get(analysis, "target_quote_integrity_status", "not_available")).lower()
    text_integrity = str(_get(analysis, "text_integrity_status", "not_checked")).lower()
    official_status = str(_get(analysis, "official_source_status", "not_available")).lower()
    source_tier = str(_get(analysis, "source_tier", "unknown")).lower()
    flags = {
        str(item).strip().lower()
        for item in (
            raw.get("interpretation_flags")
            or _get(analysis, "interpretation_flags", [])
            or []
        )
    }
    limitations = [
        str(item).strip()
        for item in (
            raw.get("data_limitations")
            or _get(analysis, "data_limitations", [])
            or []
        )
    ]
    prediction_count = (
        int(prediction_count)
        if prediction_count is not None
        else len(_get(analysis, "predictions", []) or [])
    )
    required_official = {str(item).strip().lower() for item in official_required_event_types}

    specs: list[tuple[str, bool, str, str]] = []

    # The order intentionally favors the most specific root cause. It is used
    # only for the primary diagnostic reason, not for recorder policy.
    supported = event_type != "market_price_update"
    specs.append(
        (
            "supported_causal_event",
            supported,
            "passed" if supported else "retrospective_market_price_update",
            f"event_type={event_type}",
        )
    )

    fresh = event_stage not in {"retrospective", "post_reaction", "recap"}
    specs.append(
        (
            "fresh_event_stage",
            fresh,
            "passed" if fresh else "retrospective_event",
            f"event_stage={event_stage or 'unknown'}",
        )
    )

    attribution_ok = attribution_status not in {"mismatch", "multi_subject", "unknown", "unresolved"}
    specs.append(
        (
            "attribution",
            attribution_ok,
            "passed" if attribution_ok else "attribution_mismatch",
            f"attribution_status={attribution_status}",
        )
    )

    specs.append(
        (
            "subject_target",
            subject_target_available,
            "passed" if subject_target_available else "subject_target_unavailable",
            "Attributed subject has an event-time target quote" if subject_target_available else "No trustworthy event-time target for actual article subject",
        )
    )

    quote_ok = quote_status in {"valid", "not_checked"}
    specs.append(
        (
            "quote_integrity",
            quote_ok,
            "passed" if quote_ok else "invalid_or_missing_target_quote",
            f"target_quote_integrity_status={quote_status}",
        )
    )

    routine_filing = "routine_filing_no_materiality" in flags or any(
        "routine form 144" in item.lower() and "forecast" in item.lower() for item in limitations
    )
    specs.append(
        (
            "filing_materiality",
            not routine_filing,
            "passed" if not routine_filing else "routine_filing_no_materiality",
            "No routine-filing materiality suppression" if not routine_filing else "Routine filing lacked execution/materiality evidence",
        )
    )

    official_ok = True
    official_reason = "not_required"
    if event_type in required_official:
        official_ok = official_status in {"attached", "verified", "official"} or source_tier == "official_primary"
        official_reason = "passed" if official_ok else "official_evidence_missing"
    specs.append(
        (
            "official_evidence",
            official_ok,
            official_reason,
            f"required={event_type in required_official}; status={official_status}; source_tier={source_tier}",
        )
    )

    specs.append(
        (
            "causal_signal",
            causal_signal,
            "passed" if causal_signal else "non_causal_signal",
            "Analysis was classified as causal" if causal_signal else "Analysis did not authorize a causal forecast",
        )
    )

    text_ok = text_integrity not in {"invalid", "failed", "rejected"}
    specs.append(
        (
            "text_integrity",
            text_ok,
            "passed" if text_ok else "text_integrity_failed",
            f"text_integrity_status={text_integrity}",
        )
    )

    specs.append(
        (
            "final_prediction_policy",
            prediction_eligible,
            "passed" if prediction_eligible else "final_policy_rejected",
            f"prediction_eligible={prediction_eligible}",
        )
    )

    predictions_created = prediction_count > 0
    specs.append(
        (
            "prediction_rows_created",
            predictions_created,
            "passed" if predictions_created else "no_prediction_rows",
            f"prediction_count={prediction_count}",
        )
    )

    primary_set = False
    decisions: list[GateDecision] = []
    for order, (name, passed, code, detail) in enumerate(specs, start=1):
        primary = False
        if not passed and not primary_set:
            primary = True
            primary_set = True
        decisions.append(
            GateDecision(
                gate_order=order,
                gate_name=name,
                passed=passed,
                reason_code=code,
                reason_detail=detail,
                is_primary_failure=primary,
            )
        )
    return decisions


def ensure_gate_audit_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(GATE_AUDIT_SCHEMA)


def write_gate_audit(
    conn: sqlite3.Connection,
    *,
    analysis_id: int,
    news_id: int,
    decisions: Iterable[GateDecision],
    policy_version: str = DIAGNOSTIC_POLICY_VERSION,
    audit_source: str = "runtime",
    evaluated_at: str | None = None,
) -> int:
    evaluated_at = evaluated_at or utc_iso()
    count = 0
    for decision in decisions:
        conn.execute(
            """
            INSERT INTO prediction_gate_audit(
                analysis_id,news_id,gate_order,gate_name,passed,reason_code,
                reason_detail,is_primary_failure,audit_source,policy_version,evaluated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(analysis_id,gate_name,policy_version) DO UPDATE SET
                news_id=excluded.news_id,
                gate_order=excluded.gate_order,
                passed=excluded.passed,
                reason_code=excluded.reason_code,
                reason_detail=excluded.reason_detail,
                is_primary_failure=excluded.is_primary_failure,
                audit_source=excluded.audit_source,
                evaluated_at=excluded.evaluated_at
            """,
            (
                analysis_id,
                news_id,
                decision.gate_order,
                decision.gate_name,
                int(decision.passed),
                decision.reason_code,
                decision.reason_detail,
                int(decision.is_primary_failure),
                audit_source,
                policy_version,
                evaluated_at,
            ),
        )
        count += 1
    return count


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def backfill_gate_audit(
    database_path: str | Path,
    *,
    official_required_event_types: Sequence[str] = (),
    policy_version: str = DIAGNOSTIC_POLICY_VERSION,
) -> dict[str, int]:
    path = Path(database_path)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        ensure_gate_audit_schema(conn)
        rows = conn.execute(
            """
            SELECT a.*,
                   (SELECT COUNT(*) FROM predictions p WHERE p.analysis_id=a.id) AS prediction_count
            FROM news_analyses a
            ORDER BY a.id
            """
        ).fetchall()
        gate_rows = 0
        for row in rows:
            raw = _parse_json(row["raw_json"], {})
            decisions = derive_gate_decisions(
                row,
                raw=raw,
                prediction_count=int(row["prediction_count"] or 0),
                official_required_event_types=official_required_event_types,
            )
            gate_rows += write_gate_audit(
                conn,
                analysis_id=int(row["id"]),
                news_id=int(row["news_id"]),
                decisions=decisions,
                policy_version=policy_version,
                audit_source="backfill",
            )
        conn.commit()
        return {"analyses": len(rows), "gate_rows": gate_rows}
    finally:
        conn.close()


class PipelineDiagnostics:
    def __init__(
        self,
        database_path: str | Path,
        *,
        official_required_event_types: Sequence[str] = (),
        policy_version: str = DIAGNOSTIC_POLICY_VERSION,
    ):
        self.path = Path(database_path)
        # Diagnostics are also invoked without starting the recorder daemon, so
        # apply additive recorder schema migrations before querying provenance.
        Database(self.path)
        self.official_required_event_types = tuple(
            str(item).strip().lower() for item in official_required_event_types
        )
        self.policy_version = policy_version

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        ensure_gate_audit_schema(conn)
        return conn

    @staticmethod
    def _count(conn: sqlite3.Connection, table: str, where: str = "", params: Sequence[Any] = ()) -> int:
        if not _table_exists(conn, table):
            return 0
        suffix = f" WHERE {where}" if where else ""
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}{suffix}", params).fetchone()[0])

    @staticmethod
    def _group_counts(conn: sqlite3.Connection, table: str, column: str) -> dict[str, int]:
        if not _table_exists(conn, table):
            return {}
        return {
            str(row[0] if row[0] is not None else "null"): int(row[1])
            for row in conn.execute(
                f"SELECT {column},COUNT(*) FROM {table} GROUP BY {column} ORDER BY COUNT(*) DESC,{column}"
            ).fetchall()
        }

    def backfill(self) -> dict[str, int]:
        return backfill_gate_audit(
            self.path,
            official_required_event_types=self.official_required_event_types,
            policy_version=self.policy_version,
        )

    def report(self, *, recent_limit: int = 20, backfill: bool = True) -> dict[str, Any]:
        backfill_result = self.backfill() if backfill else {"analyses": 0, "gate_rows": 0}
        conn = self._connect()
        try:
            counts = {
                "news_articles": self._count(conn, "news_articles"),
                "analysis_jobs": self._count(conn, "analysis_jobs"),
                "completed_analyses": self._count(conn, "news_analyses"),
                "prediction_eligible_analyses": self._count(conn, "news_analyses", "prediction_eligible=1"),
                "predictions": self._count(conn, "predictions"),
                "probability_calibration_eligible_predictions": self._count(
                    conn, "predictions", "valid_for_probability_calibration=1"
                ),
                "market_reactions": self._count(conn, "market_reactions"),
                "calibration_eligible_reactions": self._count(
                    conn, "market_reactions", "calibration_eligible=1"
                ),
                "prediction_outcomes": self._count(conn, "prediction_outcomes"),
                "calibration_profiles": self._count(conn, "calibration_profiles"),
                "official_documents": self._count(conn, "official_documents"),
                "replay_jobs": self._count(conn, "replay_jobs"),
                "replay_predictions": self._count(conn, "replay_predictions"),
            }
            primary_failures = {
                str(row["reason_code"]): int(row["n"])
                for row in conn.execute(
                    """
                    SELECT reason_code,COUNT(*) n
                    FROM prediction_gate_audit
                    WHERE policy_version=? AND is_primary_failure=1
                    GROUP BY reason_code ORDER BY n DESC,reason_code
                    """,
                    (self.policy_version,),
                ).fetchall()
            }
            all_failures = {
                str(row["reason_code"]): int(row["n"])
                for row in conn.execute(
                    """
                    SELECT reason_code,COUNT(*) n
                    FROM prediction_gate_audit
                    WHERE policy_version=? AND passed=0
                    GROUP BY reason_code ORDER BY n DESC,reason_code
                    """,
                    (self.policy_version,),
                ).fetchall()
            }
            recent = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT a.id AS analysis_id,a.news_id,n.received_at,n.headline,
                           a.event_type,a.event_subtype,a.primary_symbol,a.prediction_eligible,
                           a.reaction_observation_eligible,a.attribution_status,
                           a.target_quote_integrity_status,a.official_source_status,
                           COALESCE(g.reason_code,'passed') AS primary_rejection_code,
                           COALESCE(g.reason_detail,'') AS primary_rejection_detail,
                           (SELECT COUNT(*) FROM predictions p WHERE p.analysis_id=a.id) AS prediction_count
                    FROM news_analyses a
                    JOIN news_articles n ON n.id=a.news_id
                    LEFT JOIN prediction_gate_audit g
                      ON g.analysis_id=a.id AND g.policy_version=? AND g.is_primary_failure=1
                    ORDER BY a.id DESC LIMIT ?
                    """,
                    (self.policy_version, recent_limit),
                ).fetchall()
            ]
            recommendations: list[str] = []
            if counts["completed_analyses"] == 0:
                recommendations.append("Run the AI analyzer; no completed analyses exist.")
            elif counts["prediction_eligible_analyses"] == 0:
                recommendations.append(
                    "No fresh causal event passed the prediction gates. Keep the gates strict and improve causal/official event ingestion rather than converting retrospective market recaps into forecasts."
                )
            if counts["official_documents"] == 0:
                recommendations.append(
                    "No official documents are stored. Earnings, guidance, FOMC, CPI, PCE, and employment replay coverage requires point-in-time official-source ingestion."
                )
            if counts["replay_jobs"] == 0:
                recommendations.append(
                    "No replay jobs exist. Run replay-eligibility-report, then replay-seed for a date range that contains supported stored events."
                )
            if counts["predictions"] == 0:
                recommendations.append(
                    "Evaluation and calibration correctly remain empty until at least one immutable prediction row is created before its evaluation horizon."
                )
            return {
                "diagnostic_policy_version": self.policy_version,
                "database": str(self.path),
                "backfill": backfill_result,
                "pipeline_counts": counts,
                "analysis_jobs_by_status": self._group_counts(conn, "analysis_jobs", "status"),
                "analyses_by_event_type": self._group_counts(conn, "news_analyses", "event_type"),
                "reactions_by_usage_status": self._group_counts(conn, "market_reactions", "usage_status"),
                "primary_pre_prediction_rejection_reasons": primary_failures,
                "all_failed_gate_reasons": all_failures,
                "recent_analyses": recent,
                "recommendations": recommendations,
            }
        finally:
            conn.close()

    def official_readiness_report(self) -> dict[str, Any]:
        conn = self._connect()
        try:
            status = self._group_counts(conn, "official_documents", "status")
            source_types = self._group_counts(conn, "official_documents", "source_type")
            required_types = list(self.official_required_event_types)
            required_rows: list[dict[str, Any]] = []
            if required_types:
                placeholders = ",".join("?" for _ in required_types)
                required_rows = [
                    dict(row)
                    for row in conn.execute(
                        f"""
                        SELECT a.event_type,COUNT(*) analysis_count,
                               SUM(CASE WHEN a.official_source_status IN ('attached','verified','official')
                                         OR a.source_tier='official_primary' THEN 1 ELSE 0 END) ready_count
                        FROM news_analyses a
                        WHERE LOWER(a.event_type) IN ({placeholders})
                        GROUP BY a.event_type ORDER BY a.event_type
                        """,
                        required_types,
                    ).fetchall()
                ]
            candidate_urls = 0
            rows = conn.execute("SELECT input_snapshot_json FROM prediction_inputs").fetchall() if _table_exists(conn, "prediction_inputs") else []
            for row in rows:
                snapshot = _parse_json(row[0], {})
                candidate_urls += len(snapshot.get("official_source_urls") or []) if isinstance(snapshot, dict) else 0
            return {
                "official_required_event_types": required_types,
                "official_document_count": self._count(conn, "official_documents"),
                "documents_by_status": status,
                "documents_by_source_type": source_types,
                "analyses_requiring_official_evidence": required_rows,
                "candidate_official_urls_in_snapshots": candidate_urls,
                "ready": bool(self._count(conn, "official_documents", "status IN ('fetched','attached','verified','official')")),
            }
        finally:
            conn.close()

    def replay_eligibility_report(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        event_types: Sequence[str] = ("earnings", "guidance"),
        recent_limit: int = 50,
    ) -> dict[str, Any]:
        conn = self._connect()
        try:
            normalized = [str(item).strip().lower() for item in event_types]
            where = ["LOWER(a.event_type) IN (%s)" % ",".join("?" for _ in normalized)]
            params: list[Any] = list(normalized)
            if start:
                where.append("COALESCE(n.source_accepted_at,n.provider_published_at,n.first_seen_at,n.received_at)>=?")
                params.append(start)
            if end:
                where.append("COALESCE(n.source_accepted_at,n.provider_published_at,n.first_seen_at,n.received_at)<?")
                params.append(end)
            sql = f"""
                SELECT a.id AS analysis_id,a.news_id,n.received_at,n.published_at,
                       n.provider_published_at,n.source_accepted_at,n.first_seen_at,
                       n.content_updated_at,n.timestamp_quality,n.timestamp_source,
                       COALESCE(n.source_accepted_at,n.provider_published_at,
                                n.first_seen_at,n.received_at) AS source_available_at,
                       n.headline,
                       a.event_type,a.primary_symbol,a.prediction_eligible,a.attribution_status,
                       a.subject_target_available,a.target_quote_integrity_status,
                       a.official_source_status,
                       CASE WHEN pi.analysis_id IS NOT NULL THEN 1 ELSE 0 END AS has_snapshot,
                        CASE WHEN COALESCE(n.source_accepted_at,n.provider_published_at,
                                           n.first_seen_at,n.received_at) IS NOT NULL THEN 1 ELSE 0 END AS has_point_in_time_timestamps
                FROM news_analyses a
                JOIN news_articles n ON n.id=a.news_id
                LEFT JOIN prediction_inputs pi ON pi.analysis_id=a.id
                WHERE {' AND '.join(where)}
                ORDER BY n.received_at,a.id LIMIT ?
            """
            params.append(recent_limit)
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
            eligible = 0
            reasons: dict[str, int] = {}
            for row in rows:
                row_reasons: list[str] = []
                if not row["has_snapshot"]:
                    row_reasons.append("missing_immutable_prediction_snapshot")
                if not row["has_point_in_time_timestamps"]:
                    row_reasons.append("missing_point_in_time_timestamp")
                if str(row.get("timestamp_quality") or "receipt_only").lower() == "receipt_only":
                    row_reasons.append("timestamp_quality_receipt_only")
                if str(row["attribution_status"]).lower() in {"mismatch", "multi_subject", "unknown", "unresolved"}:
                    row_reasons.append("attribution_not_resolved")
                if not int(row["subject_target_available"]):
                    row_reasons.append("subject_target_unavailable")
                # Replay can re-run a previously rejected forecast, so original
                # prediction_eligible is reported but is not itself a blocker.
                row["replay_blockers"] = row_reasons
                row["replay_seed_eligible"] = not row_reasons
                if row["replay_seed_eligible"]:
                    eligible += 1
                for reason in row_reasons:
                    reasons[reason] = reasons.get(reason, 0) + 1
            return {
                "event_types": normalized,
                "start": start,
                "end": end,
                "candidate_count": len(rows),
                "replay_seed_eligible_count": eligible,
                "blocked_count": len(rows) - eligible,
                "blocker_counts": dict(sorted(reasons.items(), key=lambda item: (-item[1], item[0]))),
                "candidates": rows,
                "zero_candidate_explanation": (
                    "No stored analyses match the requested event types/date range. Replay cannot create historical source material that is absent from the database."
                    if not rows
                    else None
                ),
            }
        finally:
            conn.close()
