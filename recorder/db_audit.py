from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc


@dataclass(frozen=True)
class AuditFinding:
    severity: str
    code: str
    count: int
    message: str
    examples: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def audit_database(path: str | Path, *, now: datetime | None = None, example_limit: int = 5) -> dict[str, Any]:
    db_path = Path(path)
    now = now or datetime.now(UTC)
    findings: list[AuditFinding] = []
    with _connect(db_path) as conn:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        fk_rows = _rows(conn, "PRAGMA foreign_key_check")
        if integrity != "ok":
            findings.append(AuditFinding("critical", "sqlite_integrity", 1, integrity))
        if fk_rows:
            findings.append(AuditFinding("critical", "foreign_key_violation", len(fk_rows), "Foreign-key violations found", tuple(fk_rows[:example_limit])))

        tables = {
            str(row["name"]): int(conn.execute(f"SELECT COUNT(*) FROM {row['name']}").fetchone()[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }

        checks = [
            (
                "error", "complete_job_missing_analysis", "Complete AI jobs without an immutable analysis",
                """SELECT j.id job_id,j.news_id FROM analysis_jobs j LEFT JOIN news_analyses a ON a.job_id=j.id
                   WHERE j.status='complete' AND a.id IS NULL""",
            ),
            (
                "error", "analysis_job_state_mismatch", "Analyses whose job is absent or not complete",
                """SELECT a.id analysis_id,a.job_id,j.status FROM news_analyses a LEFT JOIN analysis_jobs j ON j.id=a.job_id
                   WHERE j.id IS NULL OR j.status!='complete'""",
            ),
            (
                "error", "completed_target_missing_reaction", "Completed reaction targets without a reaction row",
                """SELECT rt.id target_id,rt.news_id,rt.symbol,rt.horizon FROM reaction_targets rt
                   LEFT JOIN market_reactions mr ON mr.target_id=rt.id
                   WHERE rt.status='complete' AND mr.id IS NULL""",
            ),
            (
                "error", "reaction_for_pending_target", "Reaction rows attached to non-complete targets",
                """SELECT rt.id target_id,rt.status,mr.id reaction_id FROM reaction_targets rt
                   JOIN market_reactions mr ON mr.target_id=rt.id WHERE rt.status!='complete'""",
            ),
            (
                "error", "target_missing_eligible_baseline", "Reaction targets without an integrity-eligible baseline",
                """SELECT rt.id target_id,rt.news_id,rt.symbol,mb.quote_integrity_status,mb.reaction_baseline_eligible
                   FROM reaction_targets rt LEFT JOIN market_baselines mb
                     ON mb.news_id=rt.news_id AND mb.symbol=rt.symbol
                   WHERE mb.id IS NULL OR mb.reaction_baseline_eligible!=1 OR mb.selected_price IS NULL""",
            ),
            (
                "error", "governance_calibration_leak", "Excluded or observation-only targets marked calibration eligible",
                """SELECT id target_id,news_id,symbol,usage_status,calibration_eligible
                   FROM reaction_targets WHERE usage_status!='eligible' AND calibration_eligible!=0""",
            ),
            (
                "error", "governance_observation_leak", "Excluded targets marked observation eligible",
                """SELECT id target_id,news_id,symbol,usage_status,observation_eligible
                   FROM reaction_targets WHERE usage_status='excluded' AND observation_eligible!=0""",
            ),
            (
                "warning", "analyzed_news_still_provisional", "Analyzed news still has provisional reaction targets",
                """SELECT DISTINCT rt.news_id FROM reaction_targets rt JOIN news_analyses a ON a.news_id=rt.news_id
                   WHERE rt.usage_status='provisional'""",
            ),
            (
                "error", "mismatch_has_target_reference", "Attribution-mismatch analysis retained an attributed target reference",
                """SELECT id analysis_id,news_id,primary_symbol,target_quote_integrity_status,subject_target_available
                   FROM news_analyses WHERE attribution_status='mismatch' AND
                   (subject_target_available!=0 OR target_quote_integrity_status!='not_available_for_actual_subject')""",
            ),
            (
                "error", "ineligible_analysis_has_predictions", "Prediction-ineligible analyses with prediction rows",
                """SELECT a.id analysis_id,a.news_id,COUNT(p.id) predictions FROM news_analyses a
                   JOIN predictions p ON p.analysis_id=a.id WHERE a.prediction_eligible=0 GROUP BY a.id""",
            ),
            (
                "error", "invalid_recovery_state", "Recovery status contradicts attempt count",
                """SELECT id analysis_id,integrity_recovery_status,integrity_recovery_attempts
                   FROM news_analyses WHERE
                   (integrity_recovery_status IN ('recovered','partial','hard_failed','unavailable') AND integrity_recovery_attempts<1)
                   OR (integrity_recovery_status LIKE 'not_%' AND integrity_recovery_attempts!=0)""",
            ),
            (
                "error", "cluster_member_count_mismatch", "Event cluster member_count differs from actual members",
                """SELECT c.id cluster_id,c.member_count,COUNT(m.analysis_id) actual FROM event_clusters c
                   LEFT JOIN event_cluster_members m ON m.cluster_id=c.id GROUP BY c.id
                   HAVING c.member_count!=COUNT(m.analysis_id)""",
            ),
            (
                "warning", "unknown_cluster_family", "Clusters not classified into a v0.11 family",
                """SELECT id cluster_id,event_scope,event_type,subject_key FROM event_clusters
                   WHERE cluster_family IN ('','unknown')""",
            ),
            (
                "error", "reaction_governance_mismatch", "Reaction usage differs from its target usage",
                """SELECT mr.id reaction_id,mr.target_id,mr.usage_status reaction_usage,rt.usage_status target_usage
                   FROM market_reactions mr JOIN reaction_targets rt ON rt.id=mr.target_id
                   WHERE mr.usage_status!=rt.usage_status OR mr.calibration_eligible!=rt.calibration_eligible
                      OR mr.observation_eligible!=rt.observation_eligible""",
            ),
        ]
        unavailable_checks: list[str] = []
        for severity, code, message, sql in checks:
            try:
                examples = _rows(conn, sql)
            except sqlite3.OperationalError as exc:
                unavailable_checks.append(f"{code}: {exc}")
                continue
            if examples:
                findings.append(AuditFinding(severity, code, len(examples), message, tuple(examples[:example_limit])))
        if unavailable_checks:
            findings.append(AuditFinding(
                "info", "schema_upgrade_required", len(unavailable_checks),
                "Some v0.11 audit checks require the v0.11 schema migration",
                tuple({"check": item} for item in unavailable_checks[:example_limit]),
            ))

        json_specs = {
            "news_analyses": [
                "related_symbols_json", "bull_case_json", "bear_case_json", "data_limitations_json",
                "raw_json", "target_assets_json", "mentioned_symbols_json",
                "target_quote_integrity_reasons_json", "fact_validation_notes_json",
            ],
            "prediction_inputs": ["input_snapshot_json", "baseline_snapshot_json"],
            "market_baselines": ["quote_integrity_reasons_json"],
        }
        invalid_json: list[dict[str, Any]] = []
        for table, columns in json_specs.items():
            available = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
            for column in columns:
                if column not in available:
                    continue
                for row in conn.execute(f"SELECT rowid,{column} value FROM {table}"):
                    try:
                        json.loads(row["value"] or "null")
                    except Exception as exc:
                        invalid_json.append({"table": table, "rowid": row["rowid"], "column": column, "error": str(exc)})
        if invalid_json:
            findings.append(AuditFinding("error", "invalid_json", len(invalid_json), "Malformed JSON payloads", tuple(invalid_json[:example_limit])))

        now_iso = now.astimezone(UTC).isoformat()
        target_columns_for_due = {str(row[1]) for row in conn.execute("PRAGMA table_info(reaction_targets)")}
        usage_clause = " AND usage_status!='excluded'" if "usage_status" in target_columns_for_due else ""
        overdue = _rows(
            conn,
            f"SELECT COUNT(*) count FROM reaction_targets WHERE status='pending'{usage_clause} AND due_at<?",
            (now_iso,),
        )[0]["count"]
        if overdue:
            findings.append(AuditFinding("warning", "overdue_reaction_targets", int(overdue), "Pending reaction targets are already due; keep the recorder running or backfill them"))

        same_timestamp = int(conn.execute("SELECT COUNT(*) FROM news_articles WHERE published_at=received_at").fetchone()[0])
        news_count = int(tables.get("news_articles", 0))
        if news_count and same_timestamp / news_count >= 0.8:
            findings.append(AuditFinding("info", "published_equals_received", same_timestamp, "Most provider publication timestamps equal receipt time; retrospective classification must remain enabled"))

        status_counts = {
            "analysis_jobs": {str(r["status"]): int(r["n"]) for r in conn.execute("SELECT status,COUNT(*) n FROM analysis_jobs GROUP BY status")},
            "reaction_targets": {str(r["status"]): int(r["n"]) for r in conn.execute("SELECT status,COUNT(*) n FROM reaction_targets GROUP BY status")},
            "quote_integrity": {str(r["quote_integrity_status"]): int(r["n"]) for r in conn.execute("SELECT quote_integrity_status,COUNT(*) n FROM market_baselines GROUP BY quote_integrity_status")},
        }
        target_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(reaction_targets)")}
        if "usage_status" in target_columns:
            status_counts["reaction_usage"] = {
                str(r["usage_status"]): int(r["n"])
                for r in conn.execute("SELECT usage_status,COUNT(*) n FROM reaction_targets GROUP BY usage_status")
            }
        else:
            status_counts["reaction_usage"] = {"schema_not_migrated": int(tables.get("reaction_targets", 0))}

    severities = {level: sum(f.count for f in findings if f.severity == level) for level in ("critical", "error", "warning", "info")}
    return {
        "database": str(db_path.resolve()),
        "integrity_check": integrity,
        "table_counts": tables,
        "status_counts": status_counts,
        "severity_counts": severities,
        "findings": [finding.to_dict() for finding in findings],
    }
