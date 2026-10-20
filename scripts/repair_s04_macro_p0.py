from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from recorder.macro_event_lineage import (
    MacroEventReleaseInput,
    MacroObservationInput,
    S04P1_REGISTRY_VERSION,
    _cluster_key,
    classify_macro_payload,
    _fomc_seed_payloads,
    _macro_calendar_key,
    _macro_cluster_link_key,
    _macro_event_key,
    _macro_observation_key,
    _strict_replay_eligible,
    _surprise_basis,
    ensure_macro_event_lineage_schema,
    upsert_macro_event_release,
)
from recorder.news_lineage_v2 import MACRO_EVENT_LINEAGE_VERSION


DEFAULT_DB = REPO_ROOT / "work" / "canonical_research_backfill" / "m254_canonical_20250101_20260706_v1" / "canonical_market_events.db"
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "audit"


def utc_now() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def one(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    row = con.execute(sql, params).fetchone()
    if row is None:
        return None
    return row[0]


def rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in con.execute(sql, params).fetchall()]


def snapshot(con: sqlite3.Connection) -> dict[str, int]:
    tables = [
        "macro_release_calendar",
        "macro_event_releases",
        "macro_observations",
        "central_bank_events",
        "macro_event_cluster_links",
        "news_event_clusters",
    ]
    return {table: int(one(con, f"SELECT COUNT(*) FROM {table}") or 0) for table in tables}


def _payload_from_existing(release: sqlite3.Row, observation: sqlite3.Row | None) -> tuple[MacroEventReleaseInput, MacroObservationInput | None]:
    obs_input: MacroObservationInput | None = None
    if observation is not None:
        obs_input = MacroObservationInput(
            series_id=str(observation["series_id"] or ""),
            indicator_name=str(observation["indicator_name"] or ""),
            actual_value=observation["actual_value"],
            forecast_value=observation["forecast_value"],
            prior_value=observation["prior_value"],
            revised_prior_value=observation["revised_prior_value"],
            unit=str(observation["unit"] or ""),
            period=str(observation["period"] or ""),
            frequency=str(observation["frequency"] or ""),
            seasonal_adjustment=str(observation["seasonal_adjustment"] or ""),
            surprise_raw=observation["surprise_raw"],
            surprise_pct=observation["surprise_pct"],
            surprise_zscore=observation["surprise_zscore"],
            revision_delta=observation["revision_delta"],
            vintage_date=str(observation["vintage_date"] or ""),
            vintage_quality=str(observation["vintage_quality"] or "unknown"),
            data_source=str(observation["data_source"] or ""),
            data_version=str(observation["data_version"] or ""),
        )
    payload = MacroEventReleaseInput(
        source_system=str(release["source_system"] or ""),
        release_name=str(release["release_name"] or ""),
        country=str(release["country"] or ""),
        region=str(release["region"] or ""),
        currency=str(release["currency"] or ""),
        event_type=str(release["event_type"] or "macro_data_release"),
        scheduled_at_utc=str(release["scheduled_at_utc"] or ""),
        released_at_utc=str(release["released_at_utc"] or ""),
        available_at_utc=str(release["available_at_utc"] or ""),
        model_visible_at_utc=str(release["model_visible_at_utc"] or ""),
        release_time_quality=str(release["release_time_quality"] or "unknown"),
        importance=str(release["importance"] or "unknown"),
        source_calendar="existing_s04_macro_event_releases",
        calendar_version=MACRO_EVENT_LINEAGE_VERSION,
        source_url=str(release["source_url"] or ""),
        observations=(obs_input,) if obs_input is not None else (),
    )
    return payload, obs_input


def repair(db_path: Path, out_dir: Path, *, start: str, end: str, no_backup: bool = False) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utc_now()
    backup_path: str | None = None
    if not no_backup:
        backup_dir = db_path.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"before_s04_macro_p0_repair_{stamp}_{db_path.name}"
        shutil.copy2(db_path, backup)
        backup_path = str(backup)

    with connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        ensure_macro_event_lineage_schema(con)
        before = snapshot(con)

        releases = con.execute(
            """SELECT *
                 FROM macro_event_releases
                ORDER BY macro_event_id"""
        ).fetchall()
        release_updates = 0
        observation_updates = 0
        collision_rows: list[dict[str, Any]] = []
        seen_calendar_keys: dict[str, int] = {}
        seen_macro_event_keys: dict[str, int] = {}
        seen_macro_observation_keys: dict[str, int] = {}

        for release in releases:
            observation = con.execute(
                """SELECT *
                     FROM macro_observations
                    WHERE macro_event_id=?
                    ORDER BY macro_observation_id
                    LIMIT 1""",
                (release["macro_event_id"],),
            ).fetchone()
            payload, _ = _payload_from_existing(release, observation)
            policy = classify_macro_payload(payload)
            event_type = policy.event_type
            eligibility = {
                "event_cluster_eligible": policy.event_cluster_eligible,
                "reaction_trigger_eligible": policy.reaction_trigger_eligible,
                "regime_feature_eligible": policy.regime_feature_eligible,
                "intraday_label_eligible": policy.intraday_label_eligible,
                "event_eligibility_reason": policy.event_eligibility_reason,
            }
            macro_event_key = _macro_event_key(payload, event_type)
            macro_calendar_key = _macro_calendar_key(payload, event_type)
            if release["calendar_event_id"] is not None:
                duplicate_calendar_id = seen_calendar_keys.get(macro_calendar_key)
                if duplicate_calendar_id is not None and duplicate_calendar_id != int(release["calendar_event_id"]):
                    collision_rows.append(
                        {
                            "calendar_event_id": int(release["calendar_event_id"]),
                            "duplicate_calendar_id": int(duplicate_calendar_id),
                            "macro_calendar_key": macro_calendar_key,
                        }
                    )
                    macro_calendar_key = f"{macro_calendar_key}:legacy:{release['calendar_event_id']}"
                else:
                    seen_calendar_keys[macro_calendar_key] = int(release["calendar_event_id"])
            duplicate_release_id = seen_macro_event_keys.get(macro_event_key)
            if duplicate_release_id is not None:
                collision_rows.append(
                    {
                        "macro_event_id": int(release["macro_event_id"]),
                        "duplicate_of": int(duplicate_release_id),
                        "macro_event_key": macro_event_key,
                    }
                )
                macro_event_key = f"{macro_event_key}:legacy:{release['macro_event_id']}"
            else:
                seen_macro_event_keys[macro_event_key] = int(release["macro_event_id"])

            con.execute(
                """UPDATE macro_event_releases
                      SET macro_event_key=?,
                          event_type=?,
                          event_cluster_eligible=?,
                          reaction_trigger_eligible=?,
                          regime_feature_eligible=?,
                          intraday_label_eligible=?,
                          semantic_type=?,
                          registry_key=?,
                          registry_version=?,
                          release_time_policy=?,
                          release_time_quality=?,
                          event_eligibility_reason=?,
                          event_cluster_id=CASE WHEN ?=1 THEN event_cluster_id ELSE NULL END,
                          lineage_version=?
                    WHERE macro_event_id=?""",
                (
                    macro_event_key,
                    event_type,
                    eligibility["event_cluster_eligible"],
                    eligibility["reaction_trigger_eligible"],
                    eligibility["regime_feature_eligible"],
                    eligibility["intraday_label_eligible"],
                    policy.semantic_type,
                    policy.registry_key,
                    S04P1_REGISTRY_VERSION,
                    policy.release_time_policy,
                    policy.release_time_quality,
                    eligibility["event_eligibility_reason"],
                    eligibility["reaction_trigger_eligible"],
                    MACRO_EVENT_LINEAGE_VERSION,
                    release["macro_event_id"],
                ),
            )
            release_updates += con.total_changes

            if release["calendar_event_id"] is not None:
                con.execute(
                    """UPDATE macro_release_calendar
                          SET macro_calendar_key=?,
                              event_type=?
                        WHERE calendar_event_id=?""",
                    (macro_calendar_key, event_type, release["calendar_event_id"]),
                )

            if eligibility["reaction_trigger_eligible"] and release["event_cluster_id"] is not None:
                cluster_key = _cluster_key(payload, event_type)
                duplicate_cluster = con.execute(
                    """SELECT event_cluster_id
                         FROM news_event_clusters
                        WHERE cluster_key=?
                          AND event_cluster_id<>?
                        LIMIT 1""",
                    (cluster_key, release["event_cluster_id"]),
                ).fetchone()
                if duplicate_cluster is None:
                    con.execute(
                        """UPDATE news_event_clusters
                              SET cluster_key=?,
                                  reaction_trigger_eligible=1,
                                  cluster_origin=CASE WHEN cluster_origin='' THEN 'macro_event' ELSE cluster_origin END
                            WHERE event_cluster_id=?""",
                        (cluster_key, release["event_cluster_id"]),
                    )
                else:
                    collision_rows.append(
                        {
                            "macro_event_id": int(release["macro_event_id"]),
                            "event_cluster_id": int(release["event_cluster_id"]),
                            "duplicate_cluster_id": int(duplicate_cluster["event_cluster_id"]),
                            "cluster_key": cluster_key,
                        }
                    )

            observations = con.execute(
                "SELECT * FROM macro_observations WHERE macro_event_id=? ORDER BY macro_observation_id",
                (release["macro_event_id"],),
            ).fetchall()
            for obs_row in observations:
                obs = MacroObservationInput(
                    series_id=str(obs_row["series_id"] or ""),
                    indicator_name=str(obs_row["indicator_name"] or ""),
                    actual_value=obs_row["actual_value"],
                    forecast_value=obs_row["forecast_value"],
                    prior_value=obs_row["prior_value"],
                    revised_prior_value=obs_row["revised_prior_value"],
                    unit=str(obs_row["unit"] or ""),
                    period=str(obs_row["period"] or ""),
                    frequency=str(obs_row["frequency"] or ""),
                    seasonal_adjustment=str(obs_row["seasonal_adjustment"] or ""),
                    surprise_raw=obs_row["surprise_raw"],
                    surprise_pct=obs_row["surprise_pct"],
                    surprise_zscore=obs_row["surprise_zscore"],
                    revision_delta=obs_row["revision_delta"],
                    vintage_date=str(obs_row["vintage_date"] or ""),
                    vintage_quality=str(obs_row["vintage_quality"] or "unknown"),
                    data_source=str(obs_row["data_source"] or ""),
                    data_version=str(obs_row["data_version"] or ""),
                )
                obs_key = _macro_observation_key(macro_event_key, obs)
                duplicate_obs_id = seen_macro_observation_keys.get(obs_key)
                if duplicate_obs_id is not None:
                    obs_key = f"{obs_key}:legacy:{obs_row['macro_observation_id']}"
                else:
                    seen_macro_observation_keys[obs_key] = int(obs_row["macro_observation_id"])
                basis, is_consensus = _surprise_basis(obs)
                strict = _strict_replay_eligible(
                    obs,
                    available=str(release["available_at_utc"] or ""),
                    released=str(release["released_at_utc"] or ""),
                    model_visible=str(release["model_visible_at_utc"] or ""),
                )
                con.execute(
                    """UPDATE macro_observations
                          SET macro_observation_key=?,
                              strict_replay_eligible=?,
                              surprise_basis=?,
                              is_consensus_surprise=?
                        WHERE macro_observation_id=?""",
                    (obs_key, strict, basis, is_consensus, obs_row["macro_observation_id"]),
                )
                observation_updates += 1

        link_updates = 0
        links = con.execute("SELECT * FROM macro_event_cluster_links ORDER BY macro_event_id,event_cluster_id,link_type").fetchall()
        for link in links:
            link_key = _macro_cluster_link_key(int(link["macro_event_id"]), int(link["event_cluster_id"]), str(link["link_type"] or ""))
            con.execute(
                """UPDATE macro_event_cluster_links
                      SET macro_cluster_link_key=?
                    WHERE macro_event_id=? AND event_cluster_id=? AND link_type=?""",
                (link_key, link["macro_event_id"], link["event_cluster_id"], link["link_type"]),
            )
            link_updates += 1

        con.execute(
            """UPDATE macro_event_cluster_links
                  SET link_status='disabled_regime_feature_only',
                      link_reason='macro observation is regime feature, not reaction trigger'
                WHERE macro_event_id IN (
                    SELECT macro_event_id
                      FROM macro_event_releases
                     WHERE reaction_trigger_eligible=0
                       AND regime_feature_eligible=1
                )"""
        )
        disabled_links = con.total_changes
        con.execute(
            """UPDATE macro_event_cluster_links
                  SET link_status='active',
                      link_reason='reaction trigger macro event'
                WHERE macro_event_id IN (
                    SELECT macro_event_id
                      FROM macro_event_releases
                     WHERE reaction_trigger_eligible=1
                )"""
        )

        con.execute(
            """UPDATE news_event_clusters
                  SET reaction_trigger_eligible=1,
                      cluster_origin=CASE WHEN cluster_origin='' THEN 'macro_event' ELSE cluster_origin END
                WHERE event_cluster_id IN (
                    SELECT l.event_cluster_id
                      FROM macro_event_cluster_links l
                      JOIN macro_event_releases r ON r.macro_event_id=l.macro_event_id
                     WHERE r.reaction_trigger_eligible=1
                       AND COALESCE(l.link_status,'active')='active'
                )"""
        )
        con.execute(
            """UPDATE news_event_clusters
                  SET reaction_trigger_eligible=0,
                      cluster_origin=CASE WHEN cluster_origin='' THEN 'macro_regime_feature' ELSE cluster_origin END,
                      cluster_status='regime_feature_only'
                WHERE event_cluster_id IN (
                    SELECT l.event_cluster_id
                      FROM macro_event_cluster_links l
                      JOIN macro_event_releases r ON r.macro_event_id=l.macro_event_id
                     WHERE r.reaction_trigger_eligible=0
                )
                  AND event_cluster_id NOT IN (
                    SELECT event_cluster_id FROM news_article_cluster_links
                  )
                  AND event_cluster_id NOT IN (
                    SELECT l2.event_cluster_id
                      FROM macro_event_cluster_links l2
                      JOIN macro_event_releases r2 ON r2.macro_event_id=l2.macro_event_id
                     WHERE r2.reaction_trigger_eligible=1
                       AND COALESCE(l2.link_status,'active')='active'
                  )"""
        )

        fomc_payloads = _fomc_seed_payloads(start, end)
        fomc_ids = [upsert_macro_event_release(con, payload) for payload in fomc_payloads]

        after = snapshot(con)
        integrity = one(con, "PRAGMA integrity_check")
        active_non_event_links = int(
            one(
                con,
                """SELECT COUNT(*)
                     FROM macro_event_cluster_links l
                     JOIN macro_event_releases r ON r.macro_event_id=l.macro_event_id
                    WHERE r.reaction_trigger_eligible=0
                      AND COALESCE(l.link_status,'active')='active'""",
            )
            or 0
        )
        bad_consensus = int(
            one(
                con,
                """SELECT COUNT(*)
                     FROM macro_observations
                    WHERE forecast_value IS NULL
                      AND is_consensus_surprise=1""",
            )
            or 0
        )
        strict_unsafe = int(
            one(
                con,
                """SELECT COUNT(*)
                     FROM macro_observations
                    WHERE vintage_quality IN ('revised_latest_only','unknown')
                      AND strict_replay_eligible=1""",
            )
            or 0
        )
        central_bank_count = int(one(con, "SELECT COUNT(*) FROM central_bank_events") or 0)
        s05_count = int(
            one(
                con,
                """SELECT COUNT(*)
                     FROM research_backfill_stage_runs
                    WHERE stage_name LIKE 'S05%'
                       OR stage_name LIKE 'M253%'
                       OR stage_name LIKE 'M254%'
                       OR stage_name LIKE 'M255%'
                       OR stage_name LIKE 'M256%'""",
            )
            or 0
        )
        event_type_distribution = rows(
            con,
            """SELECT event_type,
                      reaction_trigger_eligible,
                      COUNT(*) AS count
                 FROM macro_event_releases
                GROUP BY event_type,reaction_trigger_eligible
                ORDER BY count DESC,event_type""",
        )
        con.commit()

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "database_path": str(db_path),
        "backup_path": backup_path,
        "window": {"start": start, "end": end},
        "before_counts": before,
        "after_counts": after,
        "fomc_seed_rows": len(fomc_payloads),
        "fomc_macro_event_ids": fomc_ids,
        "release_updates": release_updates,
        "observation_updates": observation_updates,
        "link_key_updates": link_updates,
        "disabled_link_update_total_changes_counter": disabled_links,
        "macro_event_key_collision_rows": collision_rows[:100],
        "integrity_check": integrity,
        "acceptance": {
            "active_non_event_cluster_links": active_non_event_links,
            "forecast_null_consensus_surprise_rows": bad_consensus,
            "strict_replay_unsafe_rows": strict_unsafe,
            "central_bank_events": central_bank_count,
            "s05_m253plus_stage_rows": s05_count,
        },
        "distributions": {
            "event_type": event_type_distribution,
        },
    }
    json_path = out_dir / f"s04_macro_p0_repair_{stamp}.json"
    md_path = out_dir / f"s04_macro_p0_repair_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    report["report_json"] = str(json_path)
    report["report_markdown"] = str(md_path)
    return report


def render_markdown(report: dict[str, Any]) -> str:
    acc = report["acceptance"]
    lines = [
        "# S04P0 Macro Lineage Repair Report",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- DB: `{report['database_path']}`",
        f"- Backup: `{report.get('backup_path')}`",
        f"- FOMC seed rows: `{report['fomc_seed_rows']}`",
        "",
        "## Acceptance checks",
        "",
        f"- Active non-event cluster links: `{acc['active_non_event_cluster_links']}`",
        f"- forecast NULL + consensus surprise rows: `{acc['forecast_null_consensus_surprise_rows']}`",
        f"- unsafe strict replay rows: `{acc['strict_replay_unsafe_rows']}`",
        f"- central_bank_events: `{acc['central_bank_events']}`",
        f"- S05/M253+ stage rows: `{acc['s05_m253plus_stage_rows']}`",
        f"- DB integrity: `{report['integrity_check']}`",
        "",
        "## Count diff",
        "",
        "| table | before | after | diff |",
        "|---|---:|---:|---:|",
    ]
    for table, before in report["before_counts"].items():
        after = report["after_counts"].get(table, 0)
        lines.append(f"| {table} | {before} | {after} | {after - before} |")
    lines.extend(["", "## Event type distribution", "", "| event_type | reaction_trigger_eligible | count |", "|---|---:|---:|"])
    for row in report["distributions"]["event_type"]:
        lines.append(f"| {row['event_type']} | {row['reaction_trigger_eligible']} | {row['count']} |")
    if report["macro_event_key_collision_rows"]:
        lines.extend(["", "## Macro event key collision rows", ""])
        lines.append("Collision rows were preserved with legacy-suffixed keys and require manual review.")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Repair S04 macro closeout P0 lineage/idempotency metadata.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-07-06")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    report = repair(args.db, args.out_dir, start=args.start, end=args.end, no_backup=args.no_backup)
    print(json.dumps({"report_json": report["report_json"], "report_markdown": report["report_markdown"], "acceptance": report["acceptance"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
