import json
from datetime import datetime, timezone
from freezegun import freeze_time

from recorder.analysis_database import AnalysisDatabase
from recorder.database import utc_iso
from recorder.models import NewsInput
from recorder.unknown_theme_discovery import (
    UNKNOWN_THEME_DISCOVERY_VERSION,
    UnknownThemeDiscoveryConfig,
    discover_unknown_theme_candidates,
)


UTC = timezone.utc


def _insert_news(db: AnalysisDatabase, *, headline: str, primary_symbol: str = "BROADTAPE:BZ") -> int:
    now = datetime(2026, 7, 2, 14, 0, tzinfo=UTC)
    return db.insert_news(
        NewsInput(
            provider="BZ",
            article_id=headline[:24],
            headline=headline,
            published_at=now,
            received_at=now,
            primary_symbol=primary_symbol,
            extra_data=json.dumps({"source_mode": "ibkr_broadtape"}),
        ),
        normalized_headline=headline.lower(),
        headline_hash=headline[:16],
    )


@freeze_time("2026-07-02 14:10:00")
def test_unknown_theme_discovery_queues_low_confidence_broadtape(tmp_path):
    db = AnalysisDatabase(tmp_path / "market_events.db")
    _insert_news(db, headline="Canaccord Genuity Initiates Coverage On Infleqtion with Buy Rating")

    result = discover_unknown_theme_candidates(
        UnknownThemeDiscoveryConfig(
            database_path=db.path,
            lookback_minutes=10_000,
            limit=20,
            min_group_events=1,
        )
    )

    assert result["queued_candidate_rows"] == 1
    assert result["group_count"] == 1
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM unknown_theme_candidates").fetchone()
    assert row["discovery_version"] == UNKNOWN_THEME_DISCOVERY_VERSION
    assert row["mapping_confidence"] == "low"
    assert "infleqtion" in json.loads(row["tokens_json"])


def test_unknown_theme_discovery_skips_high_confidence_energy_mapping(tmp_path):
    db = AnalysisDatabase(tmp_path / "market_events.db")
    _insert_news(db, headline="Jefferies Maintains Buy on EOG Resources, Raises Price Target to $175")

    result = discover_unknown_theme_candidates(
        UnknownThemeDiscoveryConfig(
            database_path=db.path,
            lookback_minutes=10_000,
            limit=20,
        )
    )

    assert result["queued_candidate_rows"] == 0
