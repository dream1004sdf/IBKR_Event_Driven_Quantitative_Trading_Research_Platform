from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


REPLAY_DATASET_VERSION = "replay-dataset-v1"

CSV_FIELDS = [
    "dataset_version",
    "replay_corpus_id",
    "corpus_uuid",
    "historical_event_id",
    "official_document_id",
    "symbol",
    "event_type",
    "event_subtype",
    "replay_timestamp",
    "prediction_cutoff_at",
    "session",
    "timestamp_quality",
    "source_quality",
    "leakage_status",
    "eligibility_status",
    "input_snapshot_hash",
    "official_source_type",
    "official_document_type",
    "official_document_title",
    "official_document_url",
    "official_document_available_at",
    "official_document_content_chars",
    "outcome_id",
    "price_source",
    "horizon",
    "price_status",
    "baseline_at",
    "reaction_at",
    "baseline_price",
    "reaction_price",
    "benchmark_symbol",
    "benchmark_baseline_price",
    "benchmark_reaction_price",
    "raw_return",
    "benchmark_return",
    "excess_return",
    "direction_label",
    "target_direction",
    "target_raw_return",
    "target_excess_return",
    "outcome_blocker_reason",
    "outcome_ingestion_version",
]


@dataclass(frozen=True)
class ReplayDatasetExportResult:
    output_path: Path
    manifest_path: Path
    row_count: int
    format: str
    include_input_snapshot: bool
    include_blocked: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "output_path": str(self.output_path),
            "manifest_path": str(self.manifest_path),
            "rows": self.row_count,
            "format": self.format,
            "include_input_snapshot": self.include_input_snapshot,
            "include_blocked": self.include_blocked,
        }


def _load_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(row.get("input_snapshot_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _official_document_metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    document = snapshot.get("official_document")
    if not isinstance(document, dict):
        return {
            "official_source_type": "",
            "official_document_type": "",
            "official_document_title": "",
            "official_document_url": "",
            "official_document_available_at": "",
            "official_document_content_chars": 0,
        }
    content = str(document.get("content") or "")
    return {
        "official_source_type": document.get("source_type") or "",
        "official_document_type": document.get("document_type") or "",
        "official_document_title": document.get("title") or "",
        "official_document_url": document.get("source_url") or "",
        "official_document_available_at": document.get("available_at") or "",
        "official_document_content_chars": len(content),
    }


def _base_record(row: dict[str, Any]) -> dict[str, Any]:
    snapshot = _load_snapshot(row)
    metadata = _official_document_metadata(snapshot)
    return {
        "dataset_version": REPLAY_DATASET_VERSION,
        "replay_corpus_id": row["replay_corpus_id"],
        "corpus_uuid": row["corpus_uuid"],
        "historical_event_id": row["historical_event_id"],
        "official_document_id": row["official_document_id"],
        "symbol": row["actual_subject_symbol"],
        "event_type": row["event_type"],
        "event_subtype": row["event_subtype"],
        "replay_timestamp": row["replay_timestamp"],
        "prediction_cutoff_at": row["prediction_cutoff_at"],
        "session": row["session"],
        "timestamp_quality": row["timestamp_quality"],
        "source_quality": row["source_quality"],
        "leakage_status": row["leakage_status"],
        "eligibility_status": row["eligibility_status"],
        "input_snapshot_hash": row["input_snapshot_hash"],
        **metadata,
        "outcome_id": row["outcome_id"],
        "price_source": row["price_source"],
        "horizon": row["horizon"],
        "price_status": row["price_status"],
        "baseline_at": row["baseline_at"],
        "reaction_at": row["reaction_at"],
        "baseline_price": row["baseline_price"],
        "reaction_price": row["reaction_price"],
        "benchmark_symbol": row["benchmark_symbol"],
        "benchmark_baseline_price": row["benchmark_baseline_price"],
        "benchmark_reaction_price": row["benchmark_reaction_price"],
        "raw_return": row["raw_return"],
        "benchmark_return": row["benchmark_return"],
        "excess_return": row["excess_return"],
        "direction_label": row["direction_label"],
        "target_direction": row["direction_label"] if row["price_status"] == "complete" else "unknown",
        "target_raw_return": row["raw_return"] if row["price_status"] == "complete" else None,
        "target_excess_return": row["excess_return"] if row["price_status"] == "complete" else None,
        "outcome_blocker_reason": row["outcome_blocker_reason"],
        "outcome_ingestion_version": row["outcome_ingestion_version"],
    }


class ReplayDatasetExporter:
    """Exports leakage-checked replay rows joined to separated outcome labels."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def records(self, *, include_input_snapshot: bool = False) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for row in self.rows:
            record = _base_record(row)
            if include_input_snapshot:
                record["input_snapshot"] = _load_snapshot(row)
            records.append(record)
        return records

    def export(
        self,
        output_path: Path,
        *,
        file_format: str,
        include_input_snapshot: bool = False,
        include_blocked: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> ReplayDatasetExportResult:
        normalized_format = file_format.strip().lower()
        if normalized_format not in {"csv", "jsonl"}:
            raise ValueError(f"Unsupported dataset format: {file_format}")
        output_path = output_path.expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        records = self.records(include_input_snapshot=include_input_snapshot)
        if normalized_format == "csv":
            self._write_csv(output_path, records)
        else:
            self._write_jsonl(output_path, records)
        manifest_path = output_path.with_suffix(output_path.suffix + ".manifest.json")
        self._write_manifest(
            manifest_path,
            output_path=output_path,
            records=records,
            file_format=normalized_format,
            include_input_snapshot=include_input_snapshot,
            include_blocked=include_blocked,
            filters=filters or {},
        )
        return ReplayDatasetExportResult(
            output_path=output_path,
            manifest_path=manifest_path,
            row_count=len(records),
            format=normalized_format,
            include_input_snapshot=include_input_snapshot,
            include_blocked=include_blocked,
        )

    @staticmethod
    def _write_csv(output_path: Path, records: list[dict[str, Any]]) -> None:
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(record)

    @staticmethod
    def _write_jsonl(output_path: Path, records: list[dict[str, Any]]) -> None:
        with output_path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    @staticmethod
    def _write_manifest(
        manifest_path: Path,
        *,
        output_path: Path,
        records: list[dict[str, Any]],
        file_format: str,
        include_input_snapshot: bool,
        include_blocked: bool,
        filters: dict[str, Any],
    ) -> None:
        complete_rows = sum(1 for record in records if record["price_status"] == "complete")
        blocked_rows = sum(1 for record in records if record["price_status"] != "complete")
        manifest = {
            "dataset_version": REPLAY_DATASET_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "output_path": str(output_path),
            "format": file_format,
            "rows": len(records),
            "complete_rows": complete_rows,
            "blocked_rows": blocked_rows,
            "include_input_snapshot": include_input_snapshot,
            "include_blocked": include_blocked,
            "filters": filters,
            "columns": CSV_FIELDS if file_format == "csv" else sorted(records[0].keys()) if records else [],
            "anti_leakage_note": (
                "Outcome labels are exported from replay_market_outcomes and are not part of "
                "the replay input snapshot. input_snapshot, when included, is the stored "
                "point-in-time official-source replay input."
            ),
        }
        with manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False, sort_keys=True)
