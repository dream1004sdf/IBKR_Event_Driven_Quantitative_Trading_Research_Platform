from __future__ import annotations

import json
import math
import sqlite3
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .config import EvaluationConfig
from .db import EvaluationDatabase, utc_iso
from .metrics import (
    calculate_metrics,
    confidence_bucket,
    json_compact,
    normalize_probabilities,
    predicted_direction,
    realized_direction,
)

UTC = timezone.utc
SUPPORTED_HORIZONS = {"15M", "1H", "CLOSE", "1D", "3D"}


@dataclass(frozen=True)
class EvaluationResult:
    run_id: int
    run_uuid: str
    eligible_count: int
    evaluated_count: int
    excluded_count: int
    failed_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "run_uuid": self.run_uuid,
            "eligible_count": self.eligible_count,
            "evaluated_count": self.evaluated_count,
            "excluded_count": self.excluded_count,
            "failed_count": self.failed_count,
        }


def _parse_json(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


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


def _source_class(row: Mapping[str, Any]) -> str:
    official = str(row.get("official_source_status") or "not_available").lower()
    if official == "attached":
        return "official"
    provider = str(row.get("provider") or "unknown").strip()
    return provider or str(row.get("source_tier") or "unknown")


def _attribution_valid(status: str) -> bool:
    return status.strip().lower() != "mismatch"


def _is_retrospective(row: Mapping[str, Any]) -> bool:
    if str(row.get("event_stage") or "").lower() == "retrospective":
        return True
    raw = _parse_json(str(row.get("raw_json") or "{}"))
    return str(raw.get("event_stage") or "").lower() == "retrospective"


class PredictionEvaluationEngine:
    def __init__(self, config: EvaluationConfig):
        self.config = config
        self.db = EvaluationDatabase(config.database_path)
        self.db.migrate()

    @staticmethod
    def _live_candidate_sql(full: bool) -> str:
        outcome_filter = "" if full else (
            "AND NOT EXISTS (SELECT 1 FROM prediction_outcomes po "
            "WHERE po.prediction_ref_type='live' AND po.prediction_ref_id=p.id "
            "AND po.policy_version=? AND po.eligibility_status='evaluated')"
        )
        return f"""
            SELECT
                'live' AS prediction_ref_type,
                p.id AS prediction_ref_id,
                p.id AS prediction_id,
                NULL AS replay_prediction_id,
                p.analysis_id,
                p.news_id,
                p.symbol,
                UPPER(p.horizon) AS horizon,
                p.expected_return,p.bear_return,p.bull_return,
                p.up_probability,p.flat_probability,p.down_probability,p.confidence,
                p.created_at AS prediction_created_at,
                p.created_at AS prediction_as_of,
                p.evaluation_due_at,
                p.is_valid,p.evaluation_status,p.invalid_reason,p.quality_reason,
                p.valid_for_directional_trading,p.valid_for_probability_calibration,
                a.model_name,a.prompt_version,a.event_type,a.source_tier,
                a.official_source_status,a.attribution_status,
                a.target_quote_integrity_status,a.raw_json,
                '' AS event_stage,
                n.provider,n.received_at,
                rt.id AS target_id,rt.status AS reaction_target_status,
                rt.usage_status AS target_usage_status,
                rt.calibration_eligible AS target_calibration_eligible,
                rt.due_at AS reaction_due_at,
                mr.id AS reaction_id,mr.captured_at AS reaction_captured_at,
                mr.baseline_price,mr.reaction_price,mr.raw_return,
                mr.usage_status AS reaction_usage_status,
                mr.calibration_eligible AS reaction_calibration_eligible,
                mb.quote_integrity_status AS baseline_quote_integrity_status,
                mb.reaction_baseline_eligible,
                'recorder' AS producer_type,
                'critical_event_analyzer' AS producer_name
            FROM predictions p
            JOIN news_analyses a ON a.id=p.analysis_id
            JOIN news_articles n ON n.id=p.news_id
            LEFT JOIN reaction_targets rt
              ON rt.news_id=p.news_id AND UPPER(rt.symbol)=UPPER(p.symbol)
             AND UPPER(rt.horizon)=UPPER(p.horizon)
            LEFT JOIN market_reactions mr ON mr.target_id=rt.id
            LEFT JOIN market_baselines mb
              ON mb.news_id=p.news_id AND UPPER(mb.symbol)=UPPER(p.symbol)
            WHERE 1=1 {outcome_filter}
            ORDER BY p.id
        """

    @staticmethod
    def _replay_candidate_sql(full: bool) -> str:
        outcome_filter = "" if full else (
            "AND NOT EXISTS (SELECT 1 FROM prediction_outcomes po "
            "WHERE po.prediction_ref_type='replay' AND po.prediction_ref_id=rp.id "
            "AND po.policy_version=? AND po.eligibility_status='evaluated')"
        )
        return f"""
            SELECT
                'replay' AS prediction_ref_type,
                rp.id AS prediction_ref_id,
                NULL AS prediction_id,
                rp.id AS replay_prediction_id,
                NULL AS analysis_id,
                rp.news_id,
                rp.symbol,
                UPPER(rp.horizon) AS horizon,
                rp.expected_return,rp.bear_return,rp.bull_return,
                rp.up_probability,rp.flat_probability,rp.down_probability,rp.confidence,
                rp.created_at AS prediction_created_at,
                rp.prediction_as_of,
                rp.evaluation_due_at,
                rp.is_valid,rp.evaluation_status,NULL AS invalid_reason,rp.quality_reason,
                rp.valid_for_directional_trading,rp.valid_for_probability_calibration,
                ra.model_name,ra.prompt_version,ra.event_type,ra.source_tier,
                'not_available' AS official_source_status,ra.attribution_status,
                'valid' AS target_quote_integrity_status,ra.raw_json,
                ra.event_stage,
                n.provider,n.received_at,
                rt.id AS target_id,rt.status AS reaction_target_status,
                rt.usage_status AS target_usage_status,
                rt.calibration_eligible AS target_calibration_eligible,
                rt.due_at AS reaction_due_at,
                mr.id AS reaction_id,mr.captured_at AS reaction_captured_at,
                mr.baseline_price,mr.reaction_price,mr.raw_return,
                mr.usage_status AS reaction_usage_status,
                mr.calibration_eligible AS reaction_calibration_eligible,
                mb.quote_integrity_status AS baseline_quote_integrity_status,
                mb.reaction_baseline_eligible,
                'replay' AS producer_type,
                'historical_replay' AS producer_name
            FROM replay_predictions rp
            JOIN replay_analyses ra ON ra.id=rp.replay_analysis_id
            JOIN news_articles n ON n.id=rp.news_id
            LEFT JOIN reaction_targets rt
              ON rt.news_id=rp.news_id AND UPPER(rt.symbol)=UPPER(rp.symbol)
             AND UPPER(rt.horizon)=UPPER(rp.horizon)
            LEFT JOIN market_reactions mr ON mr.target_id=rt.id
            LEFT JOIN market_baselines mb
              ON mb.news_id=rp.news_id AND UPPER(mb.symbol)=UPPER(rp.symbol)
            WHERE 1=1 {outcome_filter}
            ORDER BY rp.id
        """

    def _candidates(self, conn: sqlite3.Connection, *, full: bool, include_replay: bool) -> list[dict[str, Any]]:
        params: tuple[Any, ...] = () if full else (self.config.policy_version,)
        rows = [dict(row) for row in conn.execute(self._live_candidate_sql(full), params).fetchall()]
        if include_replay:
            rows.extend(dict(row) for row in conn.execute(self._replay_candidate_sql(full), params).fetchall())
        return rows

    def _eligibility_reason(self, row: Mapping[str, Any], evaluation_as_of: datetime) -> str | None:
        if int(row.get("is_valid") or 0) != 1:
            return "invalid_prediction"
        if int(row.get("valid_for_probability_calibration") or 0) != 1:
            return "not_probability_calibration_eligible"
        if str(row.get("evaluation_status") or "").lower() not in {"valid", "calibration_only"}:
            return "prediction_evaluation_status_not_eligible"
        if _is_retrospective(row):
            return "retrospective_prediction"
        if not _attribution_valid(str(row.get("attribution_status") or "unresolved")):
            return "attribution_mismatch"
        horizon = str(row.get("horizon") or "").upper()
        if horizon not in SUPPORTED_HORIZONS:
            return "unsupported_horizon"
        if row.get("reaction_id") is None or row.get("target_id") is None:
            return "missing_reaction"
        if str(row.get("reaction_target_status") or "").lower() != "complete":
            status = str(row.get("reaction_target_status") or "missing").lower()
            if status in {"suppressed", "missed", "failed"}:
                return f"reaction_{status}"
            return "reaction_incomplete"
        if str(row.get("target_usage_status") or "").lower() != "eligible":
            return f"reaction_{str(row.get('target_usage_status') or 'excluded').lower()}"
        if str(row.get("reaction_usage_status") or "").lower() != "eligible":
            return f"reaction_{str(row.get('reaction_usage_status') or 'excluded').lower()}"
        if int(row.get("target_calibration_eligible") or 0) != 1 or int(row.get("reaction_calibration_eligible") or 0) != 1:
            return "reaction_not_calibration_eligible"
        if str(row.get("baseline_quote_integrity_status") or "").lower() != "valid":
            return "invalid_quote"
        if int(row.get("reaction_baseline_eligible") or 0) != 1:
            return "invalid_quote"
        prediction_as_of = _parse_time(str(row.get("prediction_as_of") or ""))
        prediction_created = _parse_time(str(row.get("prediction_created_at") or ""))
        due_at = _parse_time(str(row.get("reaction_due_at") or ""))
        captured_at = _parse_time(str(row.get("reaction_captured_at") or ""))
        if prediction_as_of is None or prediction_created is None:
            return "invalid_prediction_timestamp"
        if prediction_created > evaluation_as_of:
            return "prediction_after_evaluation_as_of"
        if due_at is None or captured_at is None:
            return "missing_reaction_timestamp"
        # Replay predictions are physically created today, so their point-in-time check uses
        # prediction_as_of. Live predictions use the immutable creation timestamp.
        comparison_time = prediction_as_of if row.get("prediction_ref_type") == "replay" else prediction_created
        if comparison_time > due_at or comparison_time > captured_at:
            return "prediction_after_reaction_start"
        try:
            probabilities = normalize_probabilities(
                float(row["up_probability"]),
                float(row["flat_probability"]),
                float(row["down_probability"]),
            )
        except (KeyError, TypeError, ValueError):
            return "invalid_probability"
        if not all(math.isfinite(value) for value in probabilities):
            return "invalid_probability"
        for key in ("baseline_price", "reaction_price", "raw_return", "expected_return"):
            if row.get(key) is None or not math.isfinite(float(row[key])):
                return "missing_realized_return" if key in {"baseline_price", "reaction_price", "raw_return"} else "missing_expected_return"
        if float(row["baseline_price"]) <= 0 or float(row["reaction_price"]) <= 0:
            return "invalid_quote"
        return None

    def _outcome_values(
        self, row: Mapping[str, Any], *, run_id: int, evaluation_as_of: datetime, reason: str | None
    ) -> dict[str, Any]:
        probabilities = None
        try:
            probabilities = normalize_probabilities(
                float(row.get("up_probability") or 0),
                float(row.get("flat_probability") or 0),
                float(row.get("down_probability") or 0),
            )
        except ValueError:
            probabilities = (None, None, None)

        expected = float(row["expected_return"]) if row.get("expected_return") is not None else None
        realized = float(row["raw_return"]) if row.get("raw_return") is not None else None
        evaluated = reason is None
        pred_direction = (
            predicted_direction(*probabilities)  # type: ignore[arg-type]
            if evaluated and None not in probabilities
            else None
        )
        actual_direction = (
            realized_direction(realized, self.config.flat_threshold(str(row["horizon"])))
            if evaluated and realized is not None
            else None
        )
        signed_error = expected - realized if evaluated and expected is not None and realized is not None else None
        bear = float(row["bear_return"]) if row.get("bear_return") is not None else None
        bull = float(row["bull_return"]) if row.get("bull_return") is not None else None
        range_hit = (
            int(bear <= realized <= bull)
            if evaluated and bear is not None and bull is not None and realized is not None
            else None
        )
        prediction_as_of = str(row.get("prediction_as_of") or row.get("prediction_created_at") or "")
        point_time = _parse_time(prediction_as_of)
        due_time = _parse_time(str(row.get("reaction_due_at") or ""))
        point_in_time_valid = int(point_time is not None and due_time is not None and point_time <= due_time)
        quote_integrity = str(row.get("baseline_quote_integrity_status") or "missing")
        return {
            "run_id": run_id,
            "prediction_ref_type": row["prediction_ref_type"],
            "prediction_ref_id": row["prediction_ref_id"],
            "prediction_id": row.get("prediction_id"),
            "replay_prediction_id": row.get("replay_prediction_id"),
            "analysis_id": row.get("analysis_id"),
            "news_id": row["news_id"],
            "reaction_id": row.get("reaction_id"),
            "target_id": row.get("target_id"),
            "producer_type": row["producer_type"],
            "producer_name": row["producer_name"],
            "model_name": str(row.get("model_name") or "unknown"),
            "prompt_version": str(row.get("prompt_version") or "unknown"),
            "event_type": str(row.get("event_type") or "unknown").lower(),
            "horizon": str(row.get("horizon") or "unknown").upper(),
            "symbol": str(row.get("symbol") or "unknown").upper(),
            "source_class": _source_class(row),
            "confidence_bucket": confidence_bucket(float(row.get("confidence") or 0.0)),
            "prediction_created_at": str(row.get("prediction_created_at") or ""),
            "prediction_as_of": prediction_as_of,
            "reaction_due_at": row.get("reaction_due_at"),
            "reaction_captured_at": row.get("reaction_captured_at"),
            "anchor_price": row.get("baseline_price"),
            "target_price": row.get("reaction_price"),
            "expected_return": expected,
            "realized_return": realized,
            "predicted_direction": pred_direction,
            "realized_direction": actual_direction,
            "p_up": probabilities[0],
            "p_flat": probabilities[1],
            "p_down": probabilities[2],
            "absolute_error": abs(signed_error) if signed_error is not None else None,
            "squared_error": signed_error**2 if signed_error is not None else None,
            "signed_error": signed_error,
            "bear_return": bear,
            "base_return": expected,
            "bull_return": bull,
            "scenario_range_hit": range_hit,
            "eligibility_status": "evaluated" if evaluated else "excluded",
            "exclusion_reason": reason,
            "quote_integrity": quote_integrity,
            "attribution_valid": int(_attribution_valid(str(row.get("attribution_status") or "unresolved"))),
            "point_in_time_valid": point_in_time_valid,
            "policy_version": self.config.policy_version,
            "evaluated_at": utc_iso(evaluation_as_of),
        }

    @staticmethod
    def _upsert_outcome(conn: sqlite3.Connection, values: Mapping[str, Any]) -> None:
        columns = list(values)
        placeholders = ",".join("?" for _ in columns)
        updates = ",".join(
            f"{column}=excluded.{column}"
            for column in columns
            if column not in {"prediction_ref_type", "prediction_ref_id", "policy_version"}
        )
        conn.execute(
            f"""
            INSERT INTO prediction_outcomes({','.join(columns)}) VALUES ({placeholders})
            ON CONFLICT(prediction_ref_type,prediction_ref_id,policy_version)
            DO UPDATE SET {updates}
            """,
            tuple(values[column] for column in columns),
        )

    @staticmethod
    def _segment_specs(row: Mapping[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
        model = row["model_name"]
        event = row["event_type"]
        horizon = row["horizon"]
        symbol = row["symbol"]
        source = row["source_class"]
        confidence = row["confidence_bucket"]
        producer_type = row["producer_type"]
        producer_name = row["producer_name"]
        return [
            ("global", (None,) * 9),
            ("producer", (producer_type, producer_name, None, None, None, None, None, None, None)),
            ("model", (producer_type, producer_name, model, None, None, None, None, None, None)),
            ("event", (None, None, None, None, event, None, None, None, None)),
            ("horizon", (None, None, None, None, None, horizon, None, None, None)),
            ("symbol", (None, None, None, None, None, None, symbol, None, None)),
            ("source", (None, None, None, None, None, None, None, source, None)),
            ("confidence", (None, None, None, None, None, None, None, None, confidence)),
            ("model_event_horizon", (producer_type, producer_name, model, None, event, horizon, None, None, None)),
            ("model_event_horizon_symbol", (producer_type, producer_name, model, None, event, horizon, symbol, None, None)),
        ]

    def _store_performance(self, conn: sqlite3.Connection, run_id: int, rows: list[dict[str, Any]]) -> None:
        conn.execute("DELETE FROM model_performance WHERE run_id=?", (run_id,))
        groups: dict[tuple[str, tuple[Any, ...]], list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            for spec in self._segment_specs(row):
                groups[spec].append(row)
        generated_at = utc_iso()
        for (segment_level, dimensions), group_rows in groups.items():
            metrics = calculate_metrics(
                group_rows,
                probability_clip=self.config.probability_clip,
                bins=self.config.calibration_bins,
            )
            (
                producer_type, producer_name, model_name, prompt_version,
                event_type, horizon, symbol, source_class, confidence,
            ) = dimensions
            conn.execute(
                """
                INSERT INTO model_performance(
                    run_id,policy_version,segment_level,producer_type,producer_name,
                    model_name,prompt_version,event_type,horizon,symbol,source_class,
                    confidence_bucket,sample_count,direction_accuracy,
                    up_precision,up_recall,up_f1,down_precision,down_recall,down_f1,
                    flat_precision,flat_recall,flat_f1,brier_score,log_loss,
                    calibration_error,mae,rmse,bias,return_correlation,
                    scenario_range_coverage,confusion_matrix_json,reliability_curve_json,
                    generated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,self.config.policy_version,segment_level,producer_type,producer_name,
                    model_name,prompt_version,event_type,horizon,symbol,source_class,confidence,
                    metrics.sample_count,metrics.direction_accuracy,
                    metrics.precision["up"],metrics.recall["up"],metrics.f1["up"],
                    metrics.precision["down"],metrics.recall["down"],metrics.f1["down"],
                    metrics.precision["flat"],metrics.recall["flat"],metrics.f1["flat"],
                    metrics.brier_score,metrics.log_loss,metrics.calibration_error,
                    metrics.mae,metrics.rmse,metrics.bias,metrics.return_correlation,
                    metrics.scenario_range_coverage,
                    json_compact(metrics.confusion_matrix),
                    json_compact(metrics.reliability_curve),generated_at,
                ),
            )

    def run(
        self,
        *,
        full: bool = False,
        include_replay: bool = True,
        evaluation_as_of: datetime | None = None,
    ) -> EvaluationResult:
        evaluation_as_of = (evaluation_as_of or datetime.now(UTC)).astimezone(UTC)
        run_uuid = f"eval-{evaluation_as_of:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}"
        options = json_compact({"full": full, "include_replay": include_replay})
        with self.db.connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO evaluation_runs(
                    run_uuid,run_type,status,started_at,policy_version,code_version,
                    evaluation_as_of,options_json
                ) VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    run_uuid,"full" if full else "incremental","running",utc_iso(),
                    self.config.policy_version,self.config.code_version,
                    utc_iso(evaluation_as_of),options,
                ),
            )
            run_id = int(cursor.lastrowid)

        eligible_count = evaluated_count = excluded_count = failed_count = 0
        evaluated_rows: list[dict[str, Any]] = []
        row_errors: list[str] = []
        try:
            with self.db.connect() as conn:
                candidates = self._candidates(conn, full=full, include_replay=include_replay)
            for row in candidates:
                try:
                    reason = self._eligibility_reason(row, evaluation_as_of)
                    values = self._outcome_values(
                        row, run_id=run_id, evaluation_as_of=evaluation_as_of, reason=reason
                    )
                    with self.db.connect() as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        self._upsert_outcome(conn, values)
                        conn.execute("COMMIT")
                    if reason is None:
                        eligible_count += 1
                        evaluated_count += 1
                        evaluated_rows.append(values)
                    else:
                        excluded_count += 1
                except Exception as exc:
                    failed_count += 1
                    if len(row_errors) < 20:
                        row_errors.append(
                            f"{row.get('prediction_ref_type')}:{row.get('prediction_ref_id')} "
                            f"{exc.__class__.__name__}: {exc}"
                        )
            with self.db.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._store_performance(conn, run_id, evaluated_rows)
                conn.execute(
                    """
                    UPDATE evaluation_runs SET status='complete',completed_at=?,
                        eligible_count=?,evaluated_count=?,excluded_count=?,failed_count=?,
                        error_message=?
                    WHERE id=?
                    """,
                    (
                        utc_iso(),eligible_count,evaluated_count,excluded_count,failed_count,
                        json_compact(row_errors) if row_errors else None,run_id,
                    ),
                )
                conn.execute("COMMIT")
        except Exception as exc:
            with self.db.connect() as conn:
                conn.execute(
                    "UPDATE evaluation_runs SET status='failed',completed_at=?,error_message=? WHERE id=?",
                    (utc_iso(), f"{exc.__class__.__name__}: {exc}"[:2000], run_id),
                )
            raise
        return EvaluationResult(
            run_id,run_uuid,eligible_count,evaluated_count,excluded_count,failed_count
        )

    def report(
        self,
        *,
        event_type: str | None = None,
        horizon: str | None = None,
        model: str | None = None,
        symbol: str | None = None,
        source: str | None = None,
        confidence: str | None = None,
        show_exclusions: bool = False,
    ) -> dict[str, Any]:
        clauses = ["policy_version=?"]
        params: list[Any] = [self.config.policy_version]
        filters = {
            "event_type": event_type.lower() if event_type else None,
            "horizon": horizon.upper() if horizon else None,
            "model_name": model,
            "symbol": symbol.upper() if symbol else None,
            "source_class": source,
            "confidence_bucket": confidence,
        }
        for column, value in filters.items():
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        where = " AND ".join(clauses)
        with self.db.connect() as conn:
            all_rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM prediction_outcomes WHERE {where} ORDER BY evaluated_at,id",
                    params,
                ).fetchall()
            ]
            last_run = conn.execute(
                "SELECT * FROM evaluation_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        evaluated = [row for row in all_rows if row["eligibility_status"] == "evaluated"]
        excluded = [row for row in all_rows if row["eligibility_status"] == "excluded"]
        metrics = calculate_metrics(
            evaluated,
            probability_clip=self.config.probability_clip,
            bins=self.config.calibration_bins,
        )
        exclusions: dict[str, int] = defaultdict(int)
        for row in excluded:
            exclusions[str(row.get("exclusion_reason") or "unknown")] += 1
        by_dimension: dict[str, dict[str, int]] = {}
        for dimension in ("model_name", "event_type", "horizon", "symbol", "source_class", "confidence_bucket"):
            counts: dict[str, int] = defaultdict(int)
            for row in evaluated:
                counts[str(row.get(dimension) or "unknown")] += 1
            by_dimension[dimension] = dict(sorted(counts.items()))
        result = {
            "policy_version": self.config.policy_version,
            "filters": {key: value for key, value in filters.items() if value is not None},
            "outcome_count": len(all_rows),
            "evaluated_count": len(evaluated),
            "excluded_count": len(excluded),
            "metrics": metrics.as_dict(),
            "by_dimension": by_dimension,
            "last_run": dict(last_run) if last_run else None,
        }
        if show_exclusions:
            result["exclusions"] = dict(sorted(exclusions.items(), key=lambda item: (-item[1], item[0])))
        return result
