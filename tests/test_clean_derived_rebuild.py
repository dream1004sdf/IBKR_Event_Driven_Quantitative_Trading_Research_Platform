from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from recorder.db_maintenance import clean_derived_rebuild_reset
from recorder.macro_event_lineage import MacroEventReleaseInput, upsert_macro_event_release
from recorder.news_lineage_v2 import (
    assign_article_to_event_cluster,
    record_provider_entities,
    upsert_raw_news_article,
)
from recorder.reaction_research_engine import create_reaction_universe_plan, generate_reaction_label_stubs


UTC = timezone.utc


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE news_articles (id INTEGER PRIMARY KEY, provider TEXT, headline TEXT)")
        conn.execute("INSERT INTO news_articles (provider, headline) VALUES ('BZ', 'raw survives')")
        conn.execute(
            "CREATE TABLE external_market_signals (id INTEGER PRIMARY KEY, source TEXT, external_id TEXT)"
        )
        conn.executemany(
            "INSERT INTO external_market_signals (source, external_id) VALUES (?, ?)",
            [
                ("benzinga", "bz1"),
                ("dow_jones", "dj1"),
                ("emergency_breaking_news", "em1"),
                ("marketaux", "mx1"),
                ("alpha_vantage", "av1"),
                ("gdelt", "gd1"),
            ],
        )
        for table in (
            "replay_surprise_adjustment_notes",
            "replay_model_scores",
            "replay_market_outcomes",
            "replay_corpus",
            "standing_regime_outcomes",
            "standing_market_regimes",
            "geopolitical_signal_outcomes",
            "geopolitical_adaptive_asset_map",
            "technical_indicators",
        ):
            conn.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, payload TEXT)")
            conn.execute(f"INSERT INTO {table} (payload) VALUES ('derived')")
        conn.commit()
    finally:
        conn.close()


def _count(path: Path, table: str, where: str = "", params: tuple[object, ...] = ()) -> int:
    conn = sqlite3.connect(path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0])
    finally:
        conn.close()


def test_clean_derived_reset_preserves_raw_and_marketaux_by_default(tmp_path) -> None:
    db_path = tmp_path / "market_events.db"
    _make_db(db_path)

    report = clean_derived_rebuild_reset(
        db_path,
        tmp_path / "backups",
        report_path=tmp_path / "reset.json",
        now=datetime(2026, 7, 5, 1, 2, 3, tzinfo=UTC),
    )

    assert Path(report["backup"]["backup_path"]).exists()
    assert (tmp_path / "reset.json").exists()
    assert _count(db_path, "news_articles") == 1
    assert _count(db_path, "external_market_signals", "WHERE source='marketaux'") == 1
    assert _count(db_path, "external_market_signals", "WHERE source='benzinga'") == 0
    assert _count(db_path, "external_market_signals", "WHERE source='dow_jones'") == 0
    assert _count(db_path, "external_market_signals", "WHERE source='emergency_breaking_news'") == 0
    assert _count(db_path, "external_market_signals", "WHERE source='alpha_vantage'") == 0
    assert _count(db_path, "external_market_signals", "WHERE source='gdelt'") == 0
    assert _count(db_path, "replay_model_scores") == 0
    assert _count(db_path, "standing_market_regimes") == 0

    backup_path = Path(report["backup"]["backup_path"])
    assert _count(backup_path, "external_market_signals", "WHERE source='benzinga'") == 1
    assert _count(backup_path, "replay_model_scores") == 1


def test_clean_derived_reset_can_reset_marketaux_when_requested(tmp_path) -> None:
    db_path = tmp_path / "market_events.db"
    _make_db(db_path)

    clean_derived_rebuild_reset(
        db_path,
        tmp_path / "backups",
        reset_marketaux_signals=True,
        now=datetime(2026, 7, 5, 1, 2, 3, tzinfo=UTC),
    )

    assert _count(db_path, "news_articles") == 1
    assert _count(db_path, "external_market_signals", "WHERE source='marketaux'") == 0


def test_clean_derived_reset_preserves_raw_lineage_and_clears_v2_derived(tmp_path) -> None:
    db_path = tmp_path / "market_events.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        raw_id = upsert_raw_news_article(
            conn,
            source_system="ibkr",
            provider="BZ",
            provider_article_id="bz-clean-derived-v2",
            headline="Nvidia shares jump on AI chip demand",
            published_at_utc="2026-07-06T12:00:00+00:00",
            received_at_utc="2026-07-06T12:00:00+00:00",
            ingest_mode="historical_backfill",
            route_type="broadtape",
            primary_route_symbol="BROADTAPE:BZ",
        )
        record_provider_entities(
            conn,
            news_id=raw_id,
            provider="marketaux",
            entities=[{"symbol": "NVDA", "name": "Nvidia", "type": "equity", "match_score": 0.9}],
        )
        assign_article_to_event_cluster(conn, raw_id)
        conn.commit()
    finally:
        conn.close()

    assert _count(db_path, "raw_news_articles") == 1
    assert _count(db_path, "provider_entities") == 1
    assert _count(db_path, "news_asset_candidates") > 0
    assert _count(db_path, "news_event_clusters") > 0

    clean_derived_rebuild_reset(
        db_path,
        tmp_path / "backups",
        now=datetime(2026, 7, 6, 1, 2, 3, tzinfo=UTC),
    )

    assert _count(db_path, "raw_news_articles") == 1
    assert _count(db_path, "provider_entities") == 1
    assert _count(db_path, "news_asset_candidates") == 0
    assert _count(db_path, "news_asset_mapping_runs") == 0
    assert _count(db_path, "news_event_clusters") == 0
    assert _count(db_path, "news_event_cluster_roles") == 0


def test_clean_derived_reset_preserves_macro_raw_but_clears_reaction_foundation(tmp_path) -> None:
    db_path = tmp_path / "market_events.db"
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        macro_event_id = upsert_macro_event_release(
            conn,
            MacroEventReleaseInput(
                source_system="fed",
                release_name="FOMC Decision",
                country="US",
                currency="USD",
                    event_type="central_bank_decision",
                    scheduled_at_utc="2026-07-29T18:00:00+00:00",
                    available_at_utc="2026-07-29T18:00:05+00:00",
                    release_time_quality="provider_exact",
                    central_bank="Fed",
            ),
        )
        event_cluster_id = int(
            conn.execute(
                "SELECT event_cluster_id FROM macro_event_releases WHERE macro_event_id=?",
                (macro_event_id,),
            ).fetchone()["event_cluster_id"]
        )
        plan_id = create_reaction_universe_plan(conn, event_cluster_id)
        generate_reaction_label_stubs(conn, plan_id, windows=("5m",), decision_lags_seconds=(0,))
        conn.commit()
    finally:
        conn.close()

    assert _count(db_path, "macro_event_releases") == 1
    assert _count(db_path, "macro_event_cluster_links") == 1
    assert _count(db_path, "reaction_universe_plans") == 1
    assert _count(db_path, "event_reaction_labels") > 0

    clean_derived_rebuild_reset(
        db_path,
        tmp_path / "backups",
        now=datetime(2026, 7, 6, 1, 2, 3, tzinfo=UTC),
    )

    assert _count(db_path, "macro_event_releases") == 1
    assert _count(db_path, "central_bank_events") == 1
    assert _count(db_path, "macro_event_cluster_links") == 0
    assert _count(db_path, "news_event_clusters") == 0
    assert _count(db_path, "reaction_universe_plans") == 0
    assert _count(db_path, "reaction_universe_assets") == 0
    assert _count(db_path, "event_reaction_labels") == 0
