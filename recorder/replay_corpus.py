from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from .database import parse_utc
from .prompting import stable_json

REPLAY_CORPUS_VERSION = "replay-corpus-v1"
FORBIDDEN_CORPUS_KEYS = {
    "realized_return",
    "future_return",
    "reaction_price",
    "post_event_price",
    "market_reactions",
    "prediction_outcome",
    "actual_direction",
}


@dataclass(frozen=True)
class ReplayCorpusRow:
    corpus_uuid: str
    historical_event_id: int
    official_document_id: int
    actual_subject_symbol: str
    event_type: str
    event_subtype: str
    replay_timestamp: str
    prediction_cutoff_at: str
    session: str
    input_snapshot_hash: str
    input_snapshot_json: str
    timestamp_quality: str
    source_quality: str
    leakage_status: str
    leakage_reasons: tuple[str, ...]
    eligibility_status: str
    blocker_reason: str
    ingestion_version: str = REPLAY_CORPUS_VERSION


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _walk_forbidden(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in FORBIDDEN_CORPUS_KEYS:
                findings.append(f"forbidden snapshot key {path}.{key}")
            findings.extend(_walk_forbidden(child, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            findings.extend(_walk_forbidden(child, f"{path}[{index}]"))
    return findings


def _lte(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return parse_utc(left) <= parse_utc(right)
    except Exception:
        return False


class ReplayCorpusBuilder:
    def __init__(self, database: Any) -> None:
        self.database = database

    def build(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        event_type: str | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        rows = self.database.historical_event_candidates(
            start=start,
            end=end,
            event_type=event_type,
            limit=limit,
        )
        summary: dict[str, Any] = {
            "ingestion_version": REPLAY_CORPUS_VERSION,
            "start": start,
            "end": end,
            "event_type": event_type,
            "candidate_count": len(rows),
            "created": 0,
            "updated": 0,
            "eligible": 0,
            "blocked": 0,
            "rows": [],
        }
        for source in rows:
            corpus_row = self._build_row(source)
            corpus_id, created = self.database.upsert_replay_corpus_row(corpus_row)
            if created:
                summary["created"] += 1
            else:
                summary["updated"] += 1
            if corpus_row.eligibility_status == "eligible":
                summary["eligible"] += 1
            else:
                summary["blocked"] += 1
            summary["rows"].append(
                {
                    "corpus_id": corpus_id,
                    "historical_event_id": corpus_row.historical_event_id,
                    "actual_subject_symbol": corpus_row.actual_subject_symbol,
                    "event_type": corpus_row.event_type,
                    "replay_timestamp": corpus_row.replay_timestamp,
                    "leakage_status": corpus_row.leakage_status,
                    "eligibility_status": corpus_row.eligibility_status,
                    "blocker_reason": corpus_row.blocker_reason,
                }
            )
        return summary

    def _build_row(self, source: dict[str, Any]) -> ReplayCorpusRow:
        cutoff = str(source.get("prediction_cutoff") or "")
        available_at = str(source.get("available_at") or "")
        official_available_at = str(source.get("official_available_at") or available_at)
        source_hash = str(source.get("source_hash") or source.get("official_content_hash") or "")
        snapshot = {
            "snapshot_type": "official_historical_event",
            "anti_leakage_note": "Contains only official source material available at or before prediction_cutoff_at.",
            "historical_event": {
                "id": int(source["id"]),
                "event_type": str(source["event_type"]),
                "event_subtype": str(source.get("event_subtype") or ""),
                "actual_subject_symbol": str(source.get("actual_subject_symbol") or "").upper(),
                "event_timestamp": str(source.get("event_timestamp") or ""),
                "available_at": available_at,
                "prediction_cutoff_at": cutoff,
                "market_session": str(source.get("market_session") or "unknown"),
            },
            "official_document": {
                "id": int(source["official_document_id"]),
                "source_type": str(source.get("source_type") or "official"),
                "source_url": str(source.get("source_url") or ""),
                "title": str(source.get("title") or ""),
                "status": str(source.get("official_status") or ""),
                "available_at": official_available_at,
                "accepted_at": str(source.get("accepted_at") or ""),
                "accession_number": str(source.get("accession_number") or ""),
                "form_type": str(source.get("form_type") or ""),
                "cik": str(source.get("cik") or ""),
                "company_name": str(source.get("company_name") or ""),
                "ticker": str(source.get("ticker") or ""),
                "materiality_classification": str(source.get("materiality_classification") or ""),
                "materiality_reason": str(source.get("materiality_reason") or ""),
                "content_hash": source_hash,
                "content": str(source.get("content") or ""),
            },
        }
        snapshot_json = stable_json(snapshot)
        leakage_reasons = self._leakage_reasons(snapshot, cutoff)
        blockers: list[str] = []
        historical_eligibility = str(source.get("replay_eligibility") or "review_required")
        if historical_eligibility != "eligible":
            blockers.append(f"historical_event_{historical_eligibility}")
        if not source_hash:
            blockers.append("missing_source_hash")
        if str(source.get("official_status") or "") != "fetched":
            blockers.append("official_document_not_fetched")
        if not _lte(official_available_at, cutoff):
            blockers.append("official_document_after_cutoff")
        if leakage_reasons:
            blockers.append("leakage_review_required")

        eligibility = "eligible" if not blockers else "blocked"
        return ReplayCorpusRow(
            corpus_uuid=f"corpus-{int(source['id'])}-{REPLAY_CORPUS_VERSION}",
            historical_event_id=int(source["id"]),
            official_document_id=int(source["official_document_id"]),
            actual_subject_symbol=str(source.get("actual_subject_symbol") or "").upper(),
            event_type=str(source.get("event_type") or ""),
            event_subtype=str(source.get("event_subtype") or ""),
            replay_timestamp=available_at,
            prediction_cutoff_at=cutoff,
            session=str(source.get("market_session") or "unknown"),
            input_snapshot_hash=_sha256(snapshot_json),
            input_snapshot_json=snapshot_json,
            timestamp_quality="source_accepted",
            source_quality="official_primary",
            leakage_status="pass" if not leakage_reasons else "review_required",
            leakage_reasons=tuple(leakage_reasons),
            eligibility_status=eligibility,
            blocker_reason=";".join(blockers),
        )

    def _leakage_reasons(self, snapshot: dict[str, Any], cutoff: str) -> list[str]:
        reasons = _walk_forbidden(snapshot)
        doc_available = str(snapshot["official_document"].get("available_at") or "")
        event_available = str(snapshot["historical_event"].get("available_at") or "")
        if not _lte(doc_available, cutoff):
            reasons.append("official document available after prediction cutoff")
        if not _lte(event_available, cutoff):
            reasons.append("event available after prediction cutoff")
        return reasons
