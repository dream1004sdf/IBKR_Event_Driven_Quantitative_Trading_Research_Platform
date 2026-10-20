from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from recorder.analysis_database import AnalysisDatabase
from recorder.replay_corpus import ReplayCorpusBuilder


def _official_doc(**overrides):
    values = {
        "source_type": "sec",
        "source_url": "https://www.sec.gov/Archives/example/8k.htm",
        "title": "8-K Example",
        "content": "Official financial results release.",
        "content_hash": "abc123",
        "document_type": "8-K",
        "event_type": "earnings",
        "actual_subject_symbol": "MU",
        "published_at": "2026-06-24T20:02:01+00:00",
        "accepted_at": "2026-06-24T20:02:01+00:00",
        "first_seen_at": "2026-06-24T20:02:01+00:00",
        "available_at": "2026-06-24T20:02:01+00:00",
        "external_document_id": "sec:test-accession",
        "raw_snapshot": "<html>Official financial results release.</html>",
        "materiality_classification": "material_earnings",
        "materiality_reason": "test",
        "ingestion_version": "test",
        "status": "fetched",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _historical_event(document_id: int, **overrides):
    values = {
        "event_type": "earnings",
        "event_subtype": "quarterly_results",
        "actual_subject_symbol": "MU",
        "official_document_id": document_id,
        "event_timestamp": "2026-06-24T20:02:01+00:00",
        "available_at": "2026-06-24T20:02:01+00:00",
        "source_hash": "abc123",
        "prediction_cutoff": "2026-06-24T20:02:01+00:00",
        "market_session": "after_hours",
        "replay_eligibility": "eligible",
        "replay_eligibility_reasons": (),
        "ingestion_version": "test",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_replay_corpus_build_is_idempotent_and_leakage_clean(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "corpus.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    event_id, _ = db.upsert_historical_event(_historical_event(document_id))
    builder = ReplayCorpusBuilder(db)

    first = builder.build(event_type="earnings")
    second = builder.build(event_type="earnings")

    assert first["created"] == 1
    assert second["created"] == 0
    assert second["updated"] == 1

    rows = db.recent_replay_corpus(limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row["historical_event_id"] == event_id
    assert row["leakage_status"] == "pass"
    assert row["eligibility_status"] == "eligible"
    snapshot = json.loads(row["input_snapshot_json"])
    assert "market_reactions" not in json.dumps(snapshot)
    assert "realized_return" not in json.dumps(snapshot)
    assert snapshot["official_document"]["content"] == "Official financial results release."


def test_replay_corpus_blocks_document_after_cutoff(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "corpus-blocked.db")
    document_id, _ = db.upsert_official_document(
        _official_doc(
            available_at="2026-06-24T20:10:00+00:00",
            accepted_at="2026-06-24T20:10:00+00:00",
            external_document_id="sec:late",
        )
    )
    db.upsert_historical_event(
        _historical_event(
            document_id,
            prediction_cutoff="2026-06-24T20:02:01+00:00",
            available_at="2026-06-24T20:02:01+00:00",
        )
    )

    result = ReplayCorpusBuilder(db).build(event_type="earnings")

    assert result["blocked"] == 1
    row = db.recent_replay_corpus(limit=1)[0]
    assert row["leakage_status"] == "review_required"
    assert "official_document_after_cutoff" in row["blocker_reason"]


def test_replay_corpus_filters_date_range(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "corpus-range.db")
    document_id, _ = db.upsert_official_document(_official_doc())
    db.upsert_historical_event(_historical_event(document_id))

    result = ReplayCorpusBuilder(db).build(
        start="2026-01-01T00:00:00+00:00",
        end="2026-12-31T00:00:00+00:00",
        event_type="earnings",
    )

    assert result["candidate_count"] == 1
    assert result["eligible"] == 1
