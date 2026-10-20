from __future__ import annotations

import sqlite3
from pathlib import Path

from recorder.s05_full_range_news_clone import (
    CUTOFF_END_UTC,
    CUTOFF_START_UTC,
    build_full_range_selection,
    persist_cohorts,
)


def _database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE news_event_clusters(
                event_cluster_id INTEGER PRIMARY KEY,
                cluster_type TEXT NOT NULL,
                reaction_trigger_eligible INTEGER NOT NULL
            );
            CREATE TABLE raw_news_articles(
                id INTEGER PRIMARY KEY,
                headline TEXT NOT NULL,
                source_system TEXT NOT NULL,
                provider TEXT NOT NULL,
                route_type TEXT NOT NULL,
                published_at_utc TEXT NOT NULL,
                available_at_utc TEXT NOT NULL
            );
            CREATE TABLE news_article_cluster_links(event_cluster_id INTEGER NOT NULL,news_id INTEGER NOT NULL);
            CREATE TABLE news_visibility_policy(news_id INTEGER PRIMARY KEY,model_visible_at_utc TEXT NOT NULL);
            """
        )
        rows = (
            (1, "Apple announces acquisition of Example Corp", "benzinga", "benzinga", "ticker", "2025-01-03T15:00:00+00:00"),
            (2, "FOMC raises interest rates by 25 basis points", "marketaux", "marketaux", "discovery", "2025-02-03T19:00:00+00:00"),
            (3, "Stocks rise as investors assess the week", "marketaux", "marketaux", "discovery", "2025-03-03T19:00:00+00:00"),
            (4, "FOMC raises interest rates by 25 basis points", "marketaux", "marketaux", "discovery", "2026-07-06T04:00:00+00:00"),
        )
        for cluster_id, headline, source, provider, route, visible in rows:
            conn.execute("INSERT INTO news_event_clusters VALUES(?,?,1)", (cluster_id, "news_event"))
            conn.execute(
                "INSERT INTO raw_news_articles VALUES(?,?,?,?,?,?,?)",
                (cluster_id, headline, source, provider, route, visible, visible),
            )
            conn.execute("INSERT INTO news_article_cluster_links VALUES(?,?)", (cluster_id, cluster_id))
            conn.execute("INSERT INTO news_visibility_policy VALUES(?,?)", (cluster_id, visible))


def test_full_range_selection_enforces_cutoff_and_no_cluster_overrides(tmp_path: Path) -> None:
    database = tmp_path / "canonical.db"
    _database(database)
    selection = build_full_range_selection(database)

    assert selection["cutoff"]["start_utc"] == CUTOFF_START_UTC
    assert selection["cutoff"]["end_utc"] == CUTOFF_END_UTC
    assert selection["eligible_count"] == 3
    assert selection["counts"]["cluster_specific_override_count"] == 0
    assert all(row["cluster_specific_override_used"] == 0 for row in selection["cohorts"])
    assert all(int(row["event_cluster_id"]) != 4 for row in selection["cohorts"])


def test_noncausal_rows_never_enter_full_depth_plan(tmp_path: Path) -> None:
    database = tmp_path / "canonical.db"
    _database(database)
    selection = build_full_range_selection(database)
    cohorts = {int(row["event_cluster_id"]): row for row in selection["cohorts"]}
    plan_ids = {int(row["event_cluster_id"]) for row in selection["clusters"]}

    assert cohorts[3]["sample_purpose"] == "excluded_noncausal"
    assert cohorts[3]["causal_trigger_eligible"] == 0
    assert 3 not in plan_ids
    assert cohorts[2]["proxy_candidate"] == 1
    assert cohorts[2]["proxy_eligible"] == 1


def test_cohort_persistence_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "canonical.db"
    clone = tmp_path / "clone.db"
    _database(source)
    selection = build_full_range_selection(source)
    with sqlite3.connect(clone) as conn:
        persist_cohorts(conn, selection)
        first = conn.execute("SELECT COUNT(*) FROM s05_full_range_cohorts").fetchone()[0]
        persist_cohorts(conn, selection)
        second = conn.execute("SELECT COUNT(*) FROM s05_full_range_cohorts").fetchone()[0]
    assert first == second == 3


def test_v4_is_monotonic_and_persists_generalized_exclusion_reason(tmp_path: Path) -> None:
    database = tmp_path / "canonical.db"
    clone = tmp_path / "clone.db"
    _database(database)
    with sqlite3.connect(database) as conn:
        conn.execute("INSERT INTO news_event_clusters VALUES(5,'news_event',1)")
        conn.execute(
            "INSERT INTO raw_news_articles VALUES(?,?,?,?,?,?,?)",
            (
                5,
                "Reported Earlier, Japan Interest Rate Decision Unchanged At 0.50%",
                "benzinga",
                "benzinga",
                "ticker",
                "2025-04-01T05:00:00+00:00",
                "2025-04-01T05:00:00+00:00",
            ),
        )
        conn.execute("INSERT INTO news_article_cluster_links VALUES(5,5)")
        conn.execute("INSERT INTO news_visibility_policy VALUES(5,'2025-04-01T05:00:00+00:00')")

    selection = build_full_range_selection(database, rules_version="v4")
    row = next(item for item in selection["cohorts"] if int(item["event_cluster_id"]) == 5)
    assert row["v3_proxy_eligible"] == 1
    assert row["v4_proxy_eligible"] == 0
    assert row["v4_exclusion_reason"] == "duplicate_or_repeat"
    assert int(row["event_cluster_id"]) not in {int(item["event_cluster_id"]) for item in selection["clusters"]}

    with sqlite3.connect(clone) as conn:
        persist_cohorts(conn, selection)
        stored = conn.execute(
            "SELECT v3_proxy_eligible,v4_proxy_eligible,exclusion_reason FROM s05_full_range_v4_semantic_decisions WHERE event_cluster_id=5"
        ).fetchone()
    assert stored == (1, 0, "duplicate_or_repeat")
