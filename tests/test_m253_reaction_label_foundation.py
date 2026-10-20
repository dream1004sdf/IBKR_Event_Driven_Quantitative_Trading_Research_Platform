from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from recorder.analysis_database import AnalysisDatabase
from recorder.external_market_sources import ExternalMarketSignalCollector
from recorder.macro_event_lineage import MacroEventReleaseInput, upsert_macro_event_release
from recorder.models import NewsInput
from recorder.reaction_research_engine import (
    build_reaction_label_foundation,
    create_reaction_universe_plan,
    generate_reaction_label_stubs,
    store_event_reaction_label,
)


def _insert_news(
    db: AnalysisDatabase,
    *,
    provider: str,
    article_id: str,
    headline: str,
    primary_symbol: str,
    published_at: datetime,
) -> None:
    normalized = " ".join(headline.lower().split())
    db.insert_news(
        NewsInput(
            provider=provider,
            article_id=article_id,
            headline=headline,
            published_at=published_at,
            received_at=published_at,
            primary_symbol=primary_symbol,
            extra_data="{}",
            provider_published_at=published_at,
            first_seen_at=published_at,
        ),
        normalized,
        hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
    )


def test_reaction_foundation_builds_direct_core_event_proxy_scopes(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "m253-reaction.db")
    event_time = datetime(2026, 7, 6, 12, 0, tzinfo=UTC)
    _insert_news(
        db,
        provider="BZ",
        article_id="bz-reaction-nvda",
        headline="Nvidia shares jump as AI chip demand lifts semiconductor stocks",
        primary_symbol="BROADTAPE:BZ",
        published_at=event_time,
    )
    _insert_news(
        db,
        provider="DJNL",
        article_id="dj-reaction-nvda",
        headline="Nvidia shares rise as AI chip demand lifts sector",
        primary_symbol="NVDA",
        published_at=event_time,
    )
    collector = ExternalMarketSignalCollector(db)
    collector.convert_benzinga_news(start=event_time.date(), end=event_time.date(), keywords_only=False)
    collector.convert_dow_jones_news(start=event_time.date(), end=event_time.date(), keywords_only=False)

    payload = build_reaction_label_foundation(
        db.path,
        start="2026-07-01",
        end="2026-07-07",
        windows=("5m",),
        decision_lags_seconds=(0,),
    )

    assert payload["event_clusters_seen"] == 1
    assert payload["reaction_plans_touched"] == 1
    assert payload["reaction_labels_created"] > 0

    with sqlite3.connect(db.path) as conn:
        conn.row_factory = sqlite3.Row
        assets = [dict(row) for row in conn.execute("SELECT * FROM reaction_universe_assets").fetchall()]
        labels = [dict(row) for row in conn.execute("SELECT * FROM event_reaction_labels").fetchall()]

    assert any(row["asset_symbol"] == "NVDA" and row["label_scope"] == "direct" for row in assets)
    assert any(row["asset_symbol"] == "SPY" and row["label_scope"] == "core_context" for row in assets)
    assert any(row["asset_symbol"] == "SMH" and row["label_scope"] == "event_proxy" for row in assets)
    assert all(row["label_quality_flag"] == "pending_market_data" for row in labels)


def test_reaction_label_rejects_assets_not_in_reaction_plan(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "m253-plan-guard.db")
    with db.connect() as conn:
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

        with pytest.raises(ValueError, match="not present in reaction_universe_plan"):
            store_event_reaction_label(
                conn,
                reaction_plan_id=plan_id,
                asset_symbol="XYZ",
                asset_role="event_proxy",
                proxy_family="made_up",
                window="5m",
            )

        generated = generate_reaction_label_stubs(conn, plan_id, windows=("5m",), decision_lags_seconds=(0,))
        label_rows = [
            dict(row)
            for row in conn.execute(
                "SELECT asset_symbol,asset_role,proxy_family,window FROM event_reaction_labels"
            ).fetchall()
        ]

    assert generated > 0
    assert not any(row["asset_symbol"] == "XYZ" for row in label_rows)
    assert any(row["asset_symbol"] == "TLT" and row["proxy_family"] == "rates" for row in label_rows)


def test_date_only_macro_event_cannot_generate_intraday_labels(tmp_path: Path) -> None:
    db = AnalysisDatabase(tmp_path / "m253-date-only-macro.db")
    with db.connect() as conn:
        upsert_macro_event_release(
            conn,
            MacroEventReleaseInput(
                source_system="boj",
                release_name="BOJ monetary policy decision",
                country="JP",
                currency="JPY",
                event_type="central_bank_decision",
                scheduled_at_utc="2026-06-15T15:00:00+00:00",
                released_at_utc="2026-06-16T14:59:59+00:00",
                available_at_utc="2026-06-16T14:59:59+00:00",
                model_visible_at_utc="2026-06-16T14:59:59+00:00",
                release_time_quality="official_date_only",
                central_bank="BOJ",
            ),
        )

    payload = build_reaction_label_foundation(
        db.path,
        start="2026-06-01",
        end="2026-06-30",
        windows=("5m", "15m", "1h", "close", "1d", "5d"),
        decision_lags_seconds=(0,),
    )

    with sqlite3.connect(db.path) as conn:
        windows = {str(row[0]) for row in conn.execute("SELECT DISTINCT window FROM event_reaction_labels")}

    assert payload["event_clusters_seen"] == 1
    assert payload["suppressed_macro_window_count"] == 4
    assert windows == {"1d", "5d"}
