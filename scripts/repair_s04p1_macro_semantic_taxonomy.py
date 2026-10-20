from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.macro_event_lineage import (  # noqa: E402
    MACRO_EVENT_LINEAGE_VERSION,
    S04P1_REGISTRY_VERSION,
    MacroEventReleaseInput,
    MacroObservationInput,
    _boj_seed_payloads,
    _cluster_key,
    _ecb_seed_payloads,
    _fomc_seed_payloads,
    _macro_calendar_key,
    _macro_cluster_link_key,
    _macro_event_key,
    _macro_observation_key,
    _surprise_basis,
    classify_macro_payload,
    ensure_macro_event_lineage_schema,
    persist_macro_series_registry,
    upsert_macro_event_release,
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def parse_json(value: Any) -> dict[str, Any]:
    try:
        payload = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def row_count(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    return int(row[0] or 0) if row else 0


def snapshot(conn: sqlite3.Connection) -> dict[str, int]:
    tables = (
        "macro_series_registry",
        "macro_release_calendar",
        "macro_event_releases",
        "macro_observations",
        "central_bank_events",
        "macro_event_cluster_links",
        "news_event_clusters",
    )
    return {table: row_count(conn, f"SELECT COUNT(*) FROM {table}") for table in tables}


def observation_from_row(row: sqlite3.Row) -> MacroObservationInput:
    return MacroObservationInput(
        series_id=str(row["series_id"] or ""),
        indicator_name=str(row["indicator_name"] or ""),
        actual_value=row["actual_value"],
        forecast_value=row["forecast_value"],
        prior_value=row["prior_value"],
        revised_prior_value=row["revised_prior_value"],
        unit=str(row["unit"] or ""),
        period=str(row["period"] or ""),
        frequency=str(row["frequency"] or ""),
        seasonal_adjustment=str(row["seasonal_adjustment"] or ""),
        surprise_raw=row["surprise_raw"],
        surprise_pct=row["surprise_pct"],
        surprise_zscore=row["surprise_zscore"],
        revision_delta=row["revision_delta"],
        vintage_date=str(row["vintage_date"] or ""),
        vintage_quality=str(row["vintage_quality"] or "unknown"),
        data_source=str(row["data_source"] or ""),
        data_version=str(row["data_version"] or ""),
        raw_json=parse_json(row["raw_json"]),
    )


def payload_from_release(
    conn: sqlite3.Connection,
    release: sqlite3.Row,
    observations: tuple[MacroObservationInput, ...],
) -> MacroEventReleaseInput:
    cb = conn.execute(
        "SELECT * FROM central_bank_events WHERE macro_event_id=?",
        (int(release["macro_event_id"]),),
    ).fetchone()
    cb_payload: dict[str, Any] = {}
    central_bank = ""
    if cb is not None:
        central_bank = str(cb["central_bank"] or "")
        cb_payload = {
            "decision_type": str(cb["decision_type"] or ""),
            "policy_rate_actual": cb["policy_rate_actual"],
            "policy_rate_expected": cb["policy_rate_expected"],
            "policy_rate_prior": cb["policy_rate_prior"],
            "rate_surprise_bp": cb["rate_surprise_bp"],
            "statement_text": cb["statement_text"],
            "minutes_text": cb["minutes_text"],
            "projection_json": parse_json(cb["projection_json"]),
            "speaker": str(cb["speaker"] or ""),
            "hawkish_dovish_score": cb["hawkish_dovish_score"],
            "text_classifier_version": str(cb["text_classifier_version"] or ""),
        }
    return MacroEventReleaseInput(
        source_system=str(release["source_system"] or ""),
        release_name=str(release["release_name"] or ""),
        country=str(release["country"] or ""),
        region=str(release["region"] or ""),
        currency=str(release["currency"] or ""),
        event_type=str(release["event_type"] or ""),
        scheduled_at_utc=str(release["scheduled_at_utc"] or ""),
        released_at_utc=str(release["released_at_utc"] or ""),
        available_at_utc=str(release["available_at_utc"] or ""),
        model_visible_at_utc=str(release["model_visible_at_utc"] or ""),
        release_time_quality=str(release["release_time_quality"] or "unknown"),
        importance=str(release["importance"] or "unknown"),
        expected_importance=str(release["expected_importance"] or "unknown"),
        source_calendar=str(release["source_calendar"] or ""),
        calendar_version=str(release["calendar_version"] or ""),
        source_url=str(release["source_url"] or ""),
        raw_json=parse_json(release["raw_json"]),
        observations=observations,
        central_bank=central_bank,
        central_bank_payload=cb_payload,
    )


def repair(database: Path, *, start: str, end: str, backup: bool, output_dir: Path) -> dict[str, Any]:
    if not database.exists():
        raise FileNotFoundError(database)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path: Path | None = None
    if backup:
        backup_dir = database.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path = backup_dir / f"before_s04p1_macro_semantic_taxonomy_{stamp}_{database.name}"
        shutil.copy2(database, backup_path)

    conn = sqlite3.connect(database, timeout=120, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        ensure_macro_event_lineage_schema(conn)
        before = snapshot(conn)
        conn.execute("BEGIN IMMEDIATE")
        registry_rows = persist_macro_series_registry(conn)

        releases = conn.execute(
            """SELECT r.*,
                      COALESCE(c.expected_importance,'unknown') AS expected_importance,
                      COALESCE(c.source_calendar,'') AS source_calendar,
                      COALESCE(c.calendar_version,'') AS calendar_version
                 FROM macro_event_releases r
                 LEFT JOIN macro_release_calendar c ON c.calendar_event_id=r.calendar_event_id
                ORDER BY r.macro_event_id"""
        ).fetchall()

        conn.execute("UPDATE macro_event_releases SET macro_event_key='s04p1_tmp:' || macro_event_id")
        conn.execute("UPDATE macro_release_calendar SET macro_calendar_key='s04p1_tmp:' || calendar_event_id")
        conn.execute("UPDATE macro_observations SET macro_observation_key='s04p1_tmp:' || macro_observation_id")

        seen_event_keys: dict[str, int] = {}
        seen_calendar_keys: dict[str, int] = {}
        seen_observation_keys: dict[str, int] = {}
        duplicate_lineage_rows: list[dict[str, Any]] = []
        classification_updates = 0
        observation_updates = 0

        for release in releases:
            obs_rows = conn.execute(
                "SELECT * FROM macro_observations WHERE macro_event_id=? ORDER BY macro_observation_id",
                (int(release["macro_event_id"]),),
            ).fetchall()
            observations = tuple(observation_from_row(row) for row in obs_rows)
            payload = payload_from_release(conn, release, observations)
            policy = classify_macro_payload(payload)
            base_event_key = _macro_event_key(payload, policy.event_type)
            canonical_event_id = seen_event_keys.get(base_event_key)
            if canonical_event_id is None:
                seen_event_keys[base_event_key] = int(release["macro_event_id"])
                event_key = base_event_key
                duplicate_of = None
            else:
                duplicate_of = canonical_event_id
                event_key = f"{base_event_key}:legacy_duplicate:{release['macro_event_id']}"
                duplicate_lineage_rows.append(
                    {
                        "macro_event_id": int(release["macro_event_id"]),
                        "duplicate_of": canonical_event_id,
                        "base_macro_event_key": base_event_key,
                    }
                )

            calendar_key = _macro_calendar_key(payload, policy.event_type)
            if release["calendar_event_id"] is not None:
                existing_calendar_id = seen_calendar_keys.get(calendar_key)
                if existing_calendar_id is None:
                    seen_calendar_keys[calendar_key] = int(release["calendar_event_id"])
                elif existing_calendar_id != int(release["calendar_event_id"]):
                    calendar_key = f"{calendar_key}:legacy_duplicate:{release['calendar_event_id']}"
                conn.execute(
                    "UPDATE macro_release_calendar SET macro_calendar_key=?,event_type=? WHERE calendar_event_id=?",
                    (calendar_key, policy.event_type, int(release["calendar_event_id"])),
                )

            reaction_eligible = int(policy.reaction_trigger_eligible and duplicate_of is None)
            event_cluster_id = int(release["event_cluster_id"]) if reaction_eligible and release["event_cluster_id"] else None
            eligibility_reason = policy.event_eligibility_reason
            if duplicate_of is not None:
                eligibility_reason = f"legacy_duplicate_of:{duplicate_of}"

            conn.execute(
                """UPDATE macro_event_releases
                      SET macro_event_key=?,event_type=?,release_time_quality=?,event_cluster_eligible=?,
                          reaction_trigger_eligible=?,regime_feature_eligible=?,intraday_label_eligible=?,
                          semantic_type=?,registry_key=?,registry_version=?,release_time_policy=?,
                          event_eligibility_reason=?,event_cluster_id=?,lineage_version=?
                    WHERE macro_event_id=?""",
                (
                    event_key,
                    policy.event_type,
                    policy.release_time_quality,
                    reaction_eligible,
                    reaction_eligible,
                    policy.regime_feature_eligible,
                    policy.intraday_label_eligible if reaction_eligible else 0,
                    policy.semantic_type,
                    policy.registry_key,
                    S04P1_REGISTRY_VERSION,
                    policy.release_time_policy if reaction_eligible else "regime_feature_only",
                    eligibility_reason,
                    event_cluster_id,
                    MACRO_EVENT_LINEAGE_VERSION,
                    int(release["macro_event_id"]),
                ),
            )
            classification_updates += 1

            for obs_row, obs in zip(obs_rows, observations, strict=True):
                base_obs_key = _macro_observation_key(event_key, obs)
                obs_key = base_obs_key
                if base_obs_key in seen_observation_keys:
                    obs_key = f"{base_obs_key}:legacy_duplicate:{obs_row['macro_observation_id']}"
                else:
                    seen_observation_keys[base_obs_key] = int(obs_row["macro_observation_id"])
                surprise_basis, is_consensus = _surprise_basis(obs)
                conn.execute(
                    """UPDATE macro_observations
                          SET macro_observation_key=?,surprise_basis=?,is_consensus_surprise=?
                        WHERE macro_observation_id=?""",
                    (obs_key, surprise_basis, is_consensus, int(obs_row["macro_observation_id"])),
                )
                observation_updates += 1

        conn.execute(
            """UPDATE macro_event_cluster_links
                  SET link_status='disabled_regime_feature_only',
                      link_reason='S04P1 registry policy: regime feature or legacy duplicate'
                WHERE macro_event_id IN (
                    SELECT macro_event_id FROM macro_event_releases WHERE reaction_trigger_eligible=0
                )"""
        )
        all_links = conn.execute(
            """SELECT l.macro_event_id,l.event_cluster_id,l.link_type,r.reaction_trigger_eligible
                 FROM macro_event_cluster_links l
                 JOIN macro_event_releases r ON r.macro_event_id=l.macro_event_id"""
        ).fetchall()
        for link in all_links:
            conn.execute(
                """UPDATE macro_event_cluster_links
                      SET macro_cluster_link_key=?,
                          link_status=?,
                          link_reason=?
                    WHERE macro_event_id=? AND event_cluster_id=? AND link_type=?""",
                (
                    _macro_cluster_link_key(
                        int(link["macro_event_id"]), int(link["event_cluster_id"]), str(link["link_type"] or "")
                    ),
                    "active" if int(link["reaction_trigger_eligible"] or 0) == 1 else "disabled_regime_feature_only",
                    (
                        "S04P1 canonical registry reaction trigger"
                        if int(link["reaction_trigger_eligible"] or 0) == 1
                        else "S04P1 registry policy: regime feature or legacy duplicate"
                    ),
                    int(link["macro_event_id"]),
                    int(link["event_cluster_id"]),
                    str(link["link_type"] or ""),
                ),
            )

        for payload in (
            *_fomc_seed_payloads(start, end),
            *_ecb_seed_payloads(start, end),
            *_boj_seed_payloads(start, end),
        ):
            upsert_macro_event_release(conn, payload)

        conn.execute(
            """UPDATE news_event_clusters
                  SET reaction_trigger_eligible=1,
                      cluster_status=CASE WHEN cluster_status='regime_feature_only' THEN 'candidate' ELSE cluster_status END,
                      cluster_origin=CASE WHEN cluster_origin='' THEN 'macro_event' ELSE cluster_origin END
                WHERE event_cluster_id IN (
                    SELECT event_cluster_id FROM v_canonical_reaction_trigger_macro_events
                )"""
        )
        conn.execute(
            """UPDATE news_event_clusters
                  SET reaction_trigger_eligible=0,
                      cluster_status='regime_feature_only',
                      cluster_origin='macro_regime_feature'
                WHERE cluster_type IN ('macro_release','central_bank_event')
                  AND event_cluster_id NOT IN (
                      SELECT event_cluster_id FROM v_canonical_reaction_trigger_macro_events
                  )
                  AND event_cluster_id NOT IN (
                      SELECT event_cluster_id FROM news_article_cluster_links
                  )"""
        )

        conn.commit()
        after = snapshot(conn)
        pass_checks = {
            "wti_brent_inventory_trigger_rows": row_count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases r
                     JOIN macro_observations o ON o.macro_event_id=r.macro_event_id
                    WHERE o.series_id IN ('DCOILWTICO','DCOILBRENTEU')
                      AND (r.event_type<>'energy_price_observation' OR r.reaction_trigger_eligible<>0)""",
            ),
            "blocked_regime_series_trigger_rows": row_count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases r
                     JOIN macro_observations o ON o.macro_event_id=r.macro_event_id
                    WHERE r.reaction_trigger_eligible=1
                      AND (o.series_id GLOB 'DGS*' OR o.series_id IN ('EFFR','SOFR','RRPONTSYD','VIXCLS','T10Y2Y','T10Y3M')
                           OR o.series_id GLOB 'BAML*' OR o.series_id IN ('DCOILWTICO','DCOILBRENTEU'))""",
            ),
            "intraday_non_exact_rows": row_count(
                conn,
                """SELECT COUNT(*) FROM macro_event_releases
                    WHERE intraday_label_eligible=1
                      AND release_time_quality NOT IN ('official_exact','provider_exact')""",
            ),
            "active_noncanonical_macro_links": row_count(
                conn,
                """SELECT COUNT(*) FROM macro_event_cluster_links l
                     JOIN macro_event_releases r ON r.macro_event_id=l.macro_event_id
                    WHERE COALESCE(l.link_status,'active')='active'
                      AND (r.reaction_trigger_eligible<>1 OR r.event_cluster_eligible<>1)""",
            ),
            "s05_m253plus_stage_rows": row_count(
                conn,
                """SELECT COUNT(*) FROM research_backfill_stage_runs
                    WHERE stage_name LIKE 'S05%' OR stage_name LIKE 'M253%' OR stage_name LIKE 'M254%'
                       OR stage_name LIKE 'M255%' OR stage_name LIKE 'M256%'""",
            ),
        }
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        coverage = [
            dict(row)
            for row in conn.execute(
                """SELECT central_bank,decision_type,COUNT(*) AS count
                     FROM central_bank_events
                    GROUP BY central_bank,decision_type
                    ORDER BY central_bank,decision_type"""
            ).fetchall()
        ]
        distribution = [
            dict(row)
            for row in conn.execute(
                """SELECT event_type,semantic_type,reaction_trigger_eligible,intraday_label_eligible,COUNT(*) AS count
                     FROM macro_event_releases
                    GROUP BY event_type,semantic_type,reaction_trigger_eligible,intraday_label_eligible
                    ORDER BY count DESC"""
            ).fetchall()
        ]
        verdict = "PASS" if integrity == "ok" and all(value == 0 for value in pass_checks.values()) else "FAIL"
        report = {
            "generated_at_utc": utc_now(),
            "verdict": verdict,
            "database": str(database),
            "backup": str(backup_path) if backup_path else None,
            "registry_version": S04P1_REGISTRY_VERSION,
            "build_scope": "global_macro_v1",
            "central_bank_scope": ["Fed", "ECB", "BOJ"],
            "registry_rows_touched": registry_rows,
            "classification_rows_updated": classification_updates,
            "observation_rows_updated": observation_updates,
            "legacy_duplicate_lineage_count": len(duplicate_lineage_rows),
            "legacy_duplicate_lineage_sample": duplicate_lineage_rows[:100],
            "before": before,
            "after": after,
            "pass_checks": pass_checks,
            "central_bank_coverage": coverage,
            "event_distribution": distribution,
            "integrity_check": integrity,
            "safety": {
                "broker_orders_submitted": False,
                "transmit_enabled": False,
                "live_order_routing_enabled": False,
                "live_threshold_promotion": False,
                "ibkr_market_data_fetch_performed": False,
            },
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / f"s04p1_macro_semantic_taxonomy_repair_{stamp}.json"
        md_path = output_dir / f"s04p1_macro_semantic_taxonomy_repair_{stamp}.md"
        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        lines = [
            "# S04P1 Macro Semantic Taxonomy Repair",
            "",
            f"- Verdict: **{verdict}**",
            f"- Database: `{database}`",
            f"- Backup: `{backup_path}`",
            f"- Registry: `{S04P1_REGISTRY_VERSION}`",
            "- Build scope: `global_macro_v1`",
            "- Central banks: `Fed, ECB, BOJ`",
            "- S05/M253+ started: **no**",
            "",
            "## Pass checks",
            "",
            *[f"- {key}: `{value}`" for key, value in pass_checks.items()],
            "",
            "## Safety",
            "",
            "- No broker orders, transmit, live routing, live threshold promotion, or IBKR market-data fetch was used.",
        ]
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        report["json_report"] = str(json_path)
        report["markdown_report"] = str(md_path)
        return report
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair S04P1 macro semantic taxonomy without starting S05")
    parser.add_argument("--database", required=True)
    parser.add_argument("--start", default="2025-01-01")
    parser.add_argument("--end", default="2026-07-06")
    parser.add_argument("--output-dir", default="artifacts/audit")
    parser.add_argument("--no-backup", action="store_true")
    args = parser.parse_args()
    report = repair(
        Path(args.database).resolve(),
        start=args.start,
        end=args.end,
        backup=not args.no_backup,
        output_dir=Path(args.output_dir).resolve(),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
