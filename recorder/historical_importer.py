from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .database import parse_utc, utc_iso
from .sec_edgar import SecEdgarClient, SecFiling, classify_sec_filing

UTC = timezone.utc
US_EASTERN = ZoneInfo("America/New_York")
HISTORICAL_IMPORT_VERSION = "historical-import-v1"


@dataclass(frozen=True)
class HistoricalEventRecord:
    event_type: str
    event_subtype: str
    actual_subject_symbol: str
    official_document_id: int
    event_timestamp: str
    available_at: str
    source_hash: str
    prediction_cutoff: str
    market_session: str
    replay_eligibility: str
    replay_eligibility_reasons: tuple[str, ...]
    ingestion_version: str = HISTORICAL_IMPORT_VERSION


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Expected YYYY-MM-DD date, got {value!r}") from exc


def event_matches(requested: str, classified: str) -> bool:
    requested = requested.strip().lower()
    classified = classified.strip().lower()
    if requested == "earnings":
        return classified in {"earnings", "earnings_guidance"}
    if requested == "guidance":
        return classified in {"guidance", "earnings_guidance"}
    if requested == "earnings_guidance":
        return classified == "earnings_guidance"
    return requested == classified


def event_subtype_for(classified: str) -> str:
    if classified == "earnings_guidance":
        return "quarterly_results_with_guidance"
    if classified == "earnings":
        return "quarterly_results"
    if classified == "guidance":
        return "guidance_update"
    return classified or "unknown"


def market_session_for(timestamp_iso: str) -> str:
    try:
        eastern = parse_utc(timestamp_iso).astimezone(US_EASTERN)
    except Exception:
        return "unknown"
    local_time = eastern.time()
    if time(9, 30) <= local_time < time(16, 0):
        return "regular"
    if time(4, 0) <= local_time < time(9, 30):
        return "pre_market"
    if time(16, 0) <= local_time < time(20, 0):
        return "after_hours"
    return "overnight"


def _event_timestamp(filing: SecFiling) -> str:
    if filing.accepted_at:
        return filing.accepted_at
    if filing.filing_date:
        return f"{filing.filing_date}T00:00:00+00:00"
    return utc_iso(datetime.now(UTC))


class HistoricalEventImporter:
    """Build historical event rows from immutable official documents.

    It deliberately avoids creating `news_articles` rows.  Historical replay
    can later consume these rows only after an explicit corpus-building step.
    """

    def __init__(self, database: Any, sec_client: SecEdgarClient) -> None:
        self.database = database
        self.sec_client = sec_client

    def import_sec_earnings_guidance(
        self,
        *,
        symbols: Iterable[str],
        start: date,
        end: date,
        event_type: str,
        limit_per_symbol: int = 100,
    ) -> dict[str, Any]:
        requested = event_type.strip().lower()
        if requested not in {"earnings", "guidance", "earnings_guidance"}:
            raise ValueError("M6 historical import initially supports earnings, guidance, or earnings_guidance")
        summary: dict[str, Any] = {
            "ingestion_version": HISTORICAL_IMPORT_VERSION,
            "event_type": requested,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "symbols": [],
            "discovered_filings": 0,
            "official_documents_created": 0,
            "historical_events_created": 0,
            "historical_events_updated": 0,
            "skipped_non_matching": 0,
            "errors": 0,
        }
        for raw_symbol in symbols:
            symbol = raw_symbol.strip().upper()
            if not symbol:
                continue
            symbol_result: dict[str, Any] = {"symbol": symbol, "events": [], "error": None}
            summary["symbols"].append(symbol_result)
            try:
                filings = self.sec_client.discover_filings_between(
                    symbol,
                    forms=("8-K",),
                    start=start,
                    end=end,
                    limit=limit_per_symbol,
                )
            except Exception as exc:
                symbol_result["error"] = str(exc)
                summary["errors"] += 1
                continue
            summary["discovered_filings"] += len(filings)
            for filing in filings:
                try:
                    result = self._import_filing(filing, requested)
                except Exception as exc:
                    summary["errors"] += 1
                    symbol_result["events"].append(
                        {
                            "accession_number": filing.accession_number,
                            "status": "error",
                            "error": str(exc),
                        }
                    )
                    continue
                if result is None:
                    summary["skipped_non_matching"] += 1
                    continue
                symbol_result["events"].append(result)
                if result["official_document_created"]:
                    summary["official_documents_created"] += 1
                if result["historical_event_created"]:
                    summary["historical_events_created"] += 1
                else:
                    summary["historical_events_updated"] += 1
        return summary

    def _import_filing(self, filing: SecFiling, requested_event_type: str) -> dict[str, Any] | None:
        document_id, document_created = self.database.register_sec_filing(filing)
        if self.database.sec_filing_needs_fetch(document_id):
            payload = self.sec_client.fetch_primary_document(filing)
            classification = classify_sec_filing(filing, payload.content)
            self.database.store_sec_filing_content(
                document_id,
                filing=filing,
                payload=payload,
                classification=classification,
            )
        document = self.database.official_document_by_id(document_id)
        if not document:
            raise RuntimeError(f"SEC official document #{document_id} disappeared during import")
        classified = str(document.get("event_type") or "")
        if not event_matches(requested_event_type, classified):
            return None

        available_at = str(document.get("available_at") or document.get("accepted_at") or _event_timestamp(filing))
        content_hash = str(document.get("content_hash") or "")
        reasons: list[str] = []
        if not content_hash:
            reasons.append("missing_source_hash")
        if not available_at:
            reasons.append("missing_available_at")
        if str(document.get("status") or "") != "fetched":
            reasons.append("official_document_not_fetched")
        replay_eligibility = "eligible" if not reasons else "review_required"
        stored_event_type = requested_event_type if classified == "earnings_guidance" else classified
        event = HistoricalEventRecord(
            event_type=stored_event_type,
            event_subtype=event_subtype_for(classified),
            actual_subject_symbol=str(document.get("actual_subject_symbol") or filing.ticker).upper(),
            official_document_id=int(document_id),
            event_timestamp=available_at,
            available_at=available_at,
            source_hash=content_hash,
            prediction_cutoff=available_at,
            market_session=market_session_for(available_at),
            replay_eligibility=replay_eligibility,
            replay_eligibility_reasons=tuple(reasons),
        )
        historical_event_id, event_created = self.database.upsert_historical_event(event)
        return {
            "historical_event_id": historical_event_id,
            "official_document_id": document_id,
            "official_document_created": document_created,
            "historical_event_created": event_created,
            "accession_number": filing.accession_number,
            "event_type": event.event_type,
            "event_subtype": event.event_subtype,
            "actual_subject_symbol": event.actual_subject_symbol,
            "available_at": event.available_at,
            "market_session": event.market_session,
            "replay_eligibility": event.replay_eligibility,
            "replay_eligibility_reasons": json.loads(json.dumps(event.replay_eligibility_reasons)),
        }
