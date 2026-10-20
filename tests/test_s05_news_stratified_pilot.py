from datetime import UTC, datetime

from scripts.run_s05_news_stratified_pilot import ceil_minute, markdown_report
from scripts.select_s05_news_stratified_pilot import STRATA, matches


def test_exact_second_entry_uses_next_full_minute() -> None:
    assert ceil_minute(datetime(2026, 1, 2, 15, 3, 42, tzinfo=UTC)) == datetime(
        2026, 1, 2, 15, 4, 0, tzinfo=UTC
    )
    assert ceil_minute(datetime(2026, 1, 2, 15, 3, 0, tzinfo=UTC)) == datetime(
        2026, 1, 2, 15, 3, 0, tzinfo=UTC
    )


def test_broadtape_no_direct_stratum_is_explicit() -> None:
    row = {
        "provider_families": ["ibkr_broadtape"],
        "provider_bucket": "ibkr_broadtape",
        "has_broadtape": 1,
        "direct_resolved": 0,
        "unresolved_or_ambiguous": 0,
        "primary_headline": "broad market update",
        "event_class": "market_structure.market_color",
    }
    assert "broadtape_no_direct_entity" in STRATA
    assert matches(row, "broadtape_no_direct_entity")


def test_markdown_reports_unavailable_stratum() -> None:
    report = {
        "verdict": "PASS",
        "scope": "test",
        "selection_summary": {
            "selected_count": 240,
            "counts": {
                "provider": {},
                "session": {},
                "exact_second": 1,
                "with_direct_resolved": 1,
                "with_unresolved_or_ambiguous": 1,
            },
            "unavailable_requested_strata": ["broadtape_no_direct_entity"],
        },
        "audit": {
            "counts": {"plans": 240, "assets": 430, "labels": 4730, "strict": 1541},
            "collection": {"completed_shards": 251, "planned_shards": 251, "requested_shards": 0, "failed_shards": 0},
            "checks": {"integrity_ok": True},
        },
    }
    output = markdown_report(report)
    assert "broadtape_no_direct_entity" in output
    assert "251 / 251 / 0 / 0" in output
