from __future__ import annotations

import json
import sqlite3

from recorder.s05_event_proxy_closeout import (
    EVENT_PROXY_MAP_VERSION,
    assets_for_selection,
    match_proxy_rule,
    persist_broadtape_probe_shadow,
)


def test_event_proxy_requires_hard_trigger_and_excludes_context() -> None:
    assert match_proxy_rule("US Imposes New Sanctions On Iran Oil Exports").family == "sanctions"
    assert match_proxy_rule("Major Ransomware Attack Breaches Hospital Network").family == "cybersecurity"
    assert match_proxy_rule("Top 10 Defense Stocks To Buy") is None
    assert match_proxy_rule("Oil Technical Analysis: What You Should Know") is None
    assert match_proxy_rule("Small Caps Rally After Iran Strike Pause") is None
    assert match_proxy_rule("SPDR Gold Shares Drop Amid Fed Rate Decision") is None
    assert match_proxy_rule("What Defense Stocks Stand To Gain From Iran Attacks") is None


def test_proxy_assets_carry_versioned_point_in_time_lineage() -> None:
    selection = {
        "clusters": [{
            "event_cluster_id": 1,
            "proxy_symbol": "ITA",
            "proxy_family": "defense_conflict",
            "proxy_rule_id": "proxy-defense-conflict-v1",
            "inclusion_reason": "point-in-time headline matched rule",
            "plan_as_of_utc": "2025-01-01T12:00:00+00:00",
        }]
    }
    asset = assets_for_selection(selection)[1][0]
    assert asset["asset_role"] == "event_proxy"
    assert asset["event_proxy_map_version"] == EVENT_PROXY_MAP_VERSION
    assert asset["inclusion_source"] == "event_proxy_map"
    assert asset["map_valid_from"] <= asset["plan_as_of_utc"] < asset["map_valid_to"]


def test_broadtape_shadow_is_idempotent_and_training_excluded(tmp_path) -> None:
    probe = tmp_path / "probe.json"
    probe.write_text(json.dumps({"probe": {"headlines": [{
        "provider": "BZ",
        "article_id": "BZ$1",
        "headline": "US Imposes New Sanctions On Iran Oil Exports",
        "published_at": "2026-01-01T00:00:00+00:00",
        "received_at": "2026-01-01T00:00:01+00:00",
        "primary_symbol": "BROADTAPE:BZ",
    }]}}), encoding="utf-8")
    database = tmp_path / "shadow.db"
    first = persist_broadtape_probe_shadow(probe, database)
    second = persist_broadtape_probe_shadow(probe, database)
    assert first["second"] == {"raw": 1, "proxies": 1}
    assert second["rerun_diff"] == {"raw": 0, "proxies": 0}
    assert second["contamination_rows"] == 0
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT historical_training_eligible FROM shadow_broadtape_raw").fetchone()[0] == 0
