from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO_ROOT / "work" / "canonical_research_backfill" / "m254_canonical_20250101_20260706_v1" / "canonical_market_events.db"
DEFAULT_CONFIG = REPO_ROOT / "config" / "backfill" / "canonical_20250101_20260706.yaml"
DEFAULT_OUT_DIR = REPO_ROOT / "artifacts" / "audit"

CANONICAL_COUNT_TABLES = [
    "macro_series_registry",
    "macro_release_calendar",
    "macro_event_releases",
    "macro_observations",
    "central_bank_events",
    "macro_event_cluster_links",
    "news_event_clusters",
    "raw_news_articles",
    "external_market_signals",
    "provider_entities",
    "news_asset_candidates",
]

S04_CANONICAL_COUNT_TABLES = [
    "macro_series_registry",
    "macro_release_calendar",
    "macro_event_releases",
    "macro_observations",
    "central_bank_events",
    "macro_event_cluster_links",
    "news_event_clusters",
]

NON_EVENT_TYPES = {
    "rates_market_observation",
    "fx_market_observation",
    "policy_rate_observation",
    "energy_price_observation",
    "volatility_regime_observation",
    "credit_spread_observation",
    "liquidity_regime_observation",
    "credit_regime_observation",
    "market_regime_observation",
    "macro_data_observation",
}

TRIGGER_EVENT_TYPES = {
    "macro_data_release",
    "central_bank_decision",
    "central_bank_statement",
    "central_bank_minutes",
    "central_bank_press_conference",
    "central_bank_projection",
    "central_bank_speech",
    "energy_inventory_release",
}

ENERGY_PRICE_KEYWORDS = (
    "price",
    "wti",
    "brent",
    "gasoline",
    "diesel",
    "heating oil",
    "natural gas",
)

ENERGY_INVENTORY_KEYWORDS = (
    "inventory",
    "inventories",
    "stock",
    "stocks",
    "storage",
    "supply",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def connect_rw(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def one(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> Any:
    return con.execute(sql, params).fetchone()[0]


def rows(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(r) for r in con.execute(sql, params).fetchall()]


def columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {str(r["name"]) for r in con.execute(f"PRAGMA table_info({table})")}


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return bool(
        one(
            con,
            "SELECT COUNT(*) FROM sqlite_master WHERE type IN ('table','view') AND name=?",
            (table,),
        )
    )


def snapshot_counts(con: sqlite3.Connection) -> dict[str, int]:
    out: dict[str, int] = {}
    for table in CANONICAL_COUNT_TABLES:
        out[table] = int(one(con, f"SELECT COUNT(*) FROM {table}")) if table_exists(con, table) else -1
    return out


def limited_tail(text: str, max_chars: int = 8000) -> str:
    if len(text) <= max_chars:
        return text
    return text[-max_chars:]


def classify_release(event_type: str, release_name: str) -> dict[str, Any]:
    event_type = (event_type or "").strip()
    name = (release_name or "").strip().lower()
    if event_type in TRIGGER_EVENT_TYPES:
        return {
            "event_cluster_eligible": 1,
            "reaction_trigger_eligible": 1,
            "regime_feature_eligible": 1,
            "eligibility_reason": "trigger_event_type",
        }
    if event_type in NON_EVENT_TYPES:
        return {
            "event_cluster_eligible": 0,
            "reaction_trigger_eligible": 0,
            "regime_feature_eligible": 1,
            "eligibility_reason": "regime_feature_market_observation",
        }
    if event_type == "energy_inventory_or_price_observation":
        if any(k in name for k in ENERGY_INVENTORY_KEYWORDS) and not any(k in name for k in ENERGY_PRICE_KEYWORDS):
            return {
                "event_cluster_eligible": 1,
                "reaction_trigger_eligible": 1,
                "regime_feature_eligible": 1,
                "eligibility_reason": "energy_inventory_release_heuristic",
            }
        return {
            "event_cluster_eligible": 0,
            "reaction_trigger_eligible": 0,
            "regime_feature_eligible": 1,
            "eligibility_reason": "energy_price_or_mixed_observation_heuristic",
        }
    return {
        "event_cluster_eligible": 0,
        "reaction_trigger_eligible": 0,
        "regime_feature_eligible": 1,
        "eligibility_reason": "unknown_event_type_default_feature_only",
    }


def audit_original(db_path: Path) -> dict[str, Any]:
    with connect_readonly(db_path) as con:
        counts = snapshot_counts(con)
        integrity = one(con, "PRAGMA integrity_check")
        release_cols = columns(con, "macro_event_releases")
        obs_cols = columns(con, "macro_observations")
        cluster_cols = columns(con, "news_event_clusters")
        link_cols = columns(con, "macro_event_cluster_links")

        releases_by_source_event = rows(
            con,
            """
            SELECT r.source_system, r.event_type,
                   COUNT(*) AS releases,
                   COUNT(l.event_cluster_id) AS cluster_links,
                   COUNT(DISTINCT l.event_cluster_id) AS distinct_clusters
              FROM macro_event_releases r
              LEFT JOIN macro_event_cluster_links l ON r.macro_event_id=l.macro_event_id
             GROUP BY r.source_system,r.event_type
             ORDER BY releases DESC, r.source_system, r.event_type
            """,
        )
        for item in releases_by_source_event:
            cls = classify_release(str(item.get("event_type") or ""), "")
            item.update(cls)

        non_event_cluster_links = rows(
            con,
            """
            SELECT r.event_type, COUNT(*) AS bad_cluster_links
              FROM macro_event_releases r
              JOIN macro_event_cluster_links l ON r.macro_event_id=l.macro_event_id
             WHERE r.event_type IN ('rates_market_observation','fx_market_observation','policy_rate_observation','energy_price_observation')
               AND COALESCE(l.link_status,'active')='active'
             GROUP BY r.event_type
             ORDER BY r.event_type
            """,
        )
        non_event_cluster_link_total = sum(int(r["bad_cluster_links"]) for r in non_event_cluster_links)

        energy_mix = rows(
            con,
            """
            SELECT r.release_name, r.event_type, COUNT(*) AS cnt,
                   COUNT(l.event_cluster_id) AS cluster_links
              FROM macro_event_releases r
              LEFT JOIN macro_event_cluster_links l ON r.macro_event_id=l.macro_event_id
                                             AND COALESCE(l.link_status,'active')='active'
             WHERE r.event_type IN ('energy_inventory_or_price_observation','energy_inventory_release','energy_price_observation')
             GROUP BY r.release_name,r.event_type
             ORDER BY cnt DESC, r.release_name
             LIMIT 100
            """,
        )
        energy_price_cluster_links = 0
        energy_inventory_cluster_links = 0
        energy_mixed_samples: list[dict[str, Any]] = []
        for item in energy_mix:
            cls = classify_release(str(item.get("event_type") or ""), str(item.get("release_name") or ""))
            item.update(cls)
            links = int(item.get("cluster_links") or 0)
            if cls["reaction_trigger_eligible"]:
                energy_inventory_cluster_links += links
            else:
                energy_price_cluster_links += links
                if len(energy_mixed_samples) < 20:
                    energy_mixed_samples.append(dict(item))

        pit = {
            "missing_scheduled_at_utc": int(one(con, "SELECT COUNT(*) FROM macro_event_releases WHERE scheduled_at_utc IS NULL OR scheduled_at_utc=''")),
            "missing_released_at_utc": int(one(con, "SELECT COUNT(*) FROM macro_event_releases WHERE released_at_utc IS NULL OR released_at_utc=''")),
            "missing_available_at_utc": int(one(con, "SELECT COUNT(*) FROM macro_event_releases WHERE available_at_utc IS NULL OR available_at_utc=''")),
            "missing_model_visible_at_utc": int(one(con, "SELECT COUNT(*) FROM macro_event_releases WHERE model_visible_at_utc IS NULL OR model_visible_at_utc=''")),
            "available_before_released": int(one(con, """
                SELECT COUNT(*) FROM macro_event_releases
                 WHERE available_at_utc < released_at_utc
                   AND available_at_utc IS NOT NULL AND released_at_utc IS NOT NULL
                   AND available_at_utc<>'' AND released_at_utc<>''
            """)),
        }

        macro_event_dup_sql = """
            SELECT COUNT(*) FROM (
                SELECT macro_event_key,COUNT(*) AS cnt
                  FROM macro_event_releases
                 WHERE COALESCE(macro_event_key,'')<>''
                 GROUP BY macro_event_key
                HAVING COUNT(*)>1
            )
        """ if "macro_event_key" in release_cols else """
            SELECT COUNT(*) FROM (
                SELECT source_system,release_name,country,event_type,scheduled_at_utc,COUNT(*) AS cnt
                  FROM macro_event_releases
                 GROUP BY source_system,release_name,country,event_type,scheduled_at_utc
                HAVING COUNT(*)>1
            )
        """
        macro_obs_dup_sql = """
            SELECT COUNT(*) FROM (
                SELECT macro_observation_key,COUNT(*) AS cnt
                  FROM macro_observations
                 WHERE COALESCE(macro_observation_key,'')<>''
                 GROUP BY macro_observation_key
                HAVING COUNT(*)>1
            )
        """ if "macro_observation_key" in obs_cols else """
            SELECT COUNT(*) FROM (
                SELECT macro_event_id,series_id,indicator_name,period,vintage_date,COUNT(*) AS cnt
                  FROM macro_observations
                 GROUP BY macro_event_id,series_id,indicator_name,period,vintage_date
                HAVING COUNT(*)>1
            )
        """
        duplicates = {
            "macro_event_key_duplicate_groups": int(one(con, macro_event_dup_sql)),
            "macro_observation_key_duplicate_groups": int(one(con, macro_obs_dup_sql)),
            "central_bank_key_duplicate_groups": int(one(con, """
                SELECT COUNT(*) FROM (
                    SELECT central_bank,decision_type,macro_event_id,COUNT(*) AS cnt
                      FROM central_bank_events
                     GROUP BY central_bank,decision_type,macro_event_id
                    HAVING COUNT(*)>1
                )
            """)),
        }

        fake_macro_news = int(one(con, """
            SELECT COUNT(*) FROM raw_news_articles
             WHERE lower(source_system) IN ('fred','alfred','bls','bea','eia','fed','boj','ecb')
        """))

        vintage_dist = rows(
            con,
            """
            SELECT COALESCE(vintage_quality,'<null>') AS vintage_quality, COUNT(*) AS cnt
              FROM macro_observations
             GROUP BY vintage_quality
             ORDER BY cnt DESC
            """,
        )
        strict_replay_col_exists = "strict_replay_eligible" in obs_cols
        if strict_replay_col_exists:
            strict_replay_unsafe = int(one(con, """
                SELECT COUNT(*) FROM macro_observations
                 WHERE vintage_quality IN ('revised_latest_only','unknown')
                   AND strict_replay_eligible=1
            """))
        else:
            strict_replay_unsafe = None

        surprise_basis_col_exists = "surprise_basis" in obs_cols
        consensus_col_exists = "is_consensus_surprise" in obs_cols
        actual_minus_forecast_col_exists = "actual_minus_forecast" in obs_cols
        if surprise_basis_col_exists and consensus_col_exists:
            surprise_dist = rows(
                con,
                """
                SELECT COALESCE(surprise_basis,'<null>') AS surprise_basis,
                       COALESCE(is_consensus_surprise,-1) AS is_consensus_surprise,
                       COUNT(*) AS cnt
                  FROM macro_observations
                 GROUP BY surprise_basis,is_consensus_surprise
                 ORDER BY cnt DESC
                """,
            )
            bad_consensus_surprise = int(one(con, """
                SELECT COUNT(*) FROM macro_observations
                 WHERE forecast_value IS NULL AND is_consensus_surprise=1
            """))
        else:
            surprise_dist = []
            bad_consensus_surprise = None
        if actual_minus_forecast_col_exists:
            bad_actual_minus_forecast = int(one(con, """
                SELECT COUNT(*) FROM macro_observations
                 WHERE forecast_value IS NULL AND actual_minus_forecast IS NOT NULL
            """))
        else:
            bad_actual_minus_forecast = None
        forecast_null_surprise_raw = int(one(con, """
            SELECT COUNT(*) FROM macro_observations
             WHERE forecast_value IS NULL AND surprise_raw IS NOT NULL
        """))

        central_bank_dist = rows(
            con,
            """
            SELECT COALESCE(central_bank,'<null>') AS central_bank,
                   COALESCE(decision_type,'<null>') AS decision_type,
                   COUNT(*) AS cnt
              FROM central_bank_events
             GROUP BY central_bank,decision_type
             ORDER BY central_bank,decision_type
            """,
        )
        central_bank_count = int(counts.get("central_bank_events", 0))

        sample_non_event_cluster_links = rows(
            con,
            """
            SELECT r.macro_event_id,r.source_system,r.release_name,r.event_type,r.scheduled_at_utc,
                   r.available_at_utc,l.event_cluster_id,c.cluster_key,c.primary_headline
              FROM macro_event_releases r
              JOIN macro_event_cluster_links l ON r.macro_event_id=l.macro_event_id
              JOIN news_event_clusters c ON c.event_cluster_id=l.event_cluster_id
             WHERE r.event_type IN ('rates_market_observation','fx_market_observation','policy_rate_observation','energy_price_observation')
               AND COALESCE(l.link_status,'active')='active'
             ORDER BY r.event_type,r.scheduled_at_utc
             LIMIT 20
            """,
        )
        sample_forecast_null_surprise = rows(
            con,
            """
            SELECT o.macro_observation_id,o.macro_event_id,o.series_id,o.indicator_name,o.period,
                   o.actual_value,o.forecast_value,o.prior_value,o.surprise_raw,o.revision_delta,
                   o.vintage_quality,r.source_system,r.event_type,r.release_name
              FROM macro_observations o
              JOIN macro_event_releases r ON r.macro_event_id=o.macro_event_id
             WHERE o.forecast_value IS NULL AND o.surprise_raw IS NOT NULL
             LIMIT 20
            """,
        )

        required_schema = {
            "macro_release_calendar": [
                "macro_calendar_key",
            ],
            "macro_event_releases": [
                "macro_event_key",
                "event_cluster_eligible",
                "reaction_trigger_eligible",
                "regime_feature_eligible",
                "event_eligibility_reason",
            ],
            "macro_observations": [
                "macro_observation_key",
                "strict_replay_eligible",
                "surprise_basis",
                "is_consensus_surprise",
            ],
            "macro_event_cluster_links": [
                "macro_cluster_link_key",
                "link_status",
                "link_reason",
            ],
            "news_event_clusters": [
                "reaction_trigger_eligible",
                "cluster_origin",
            ],
        }
        missing_schema = {
            table: [col for col in cols if col not in columns(con, table)]
            for table, cols in required_schema.items()
        }

        s05plus_count = int(one(con, """
            SELECT COUNT(*) FROM research_backfill_stage_runs
             WHERE stage_name LIKE 'S05_%'
                OR stage_name LIKE 'S06_%'
                OR stage_name LIKE 'S07_%'
                OR stage_name LIKE 'S08_%'
                OR stage_name LIKE 'S09_%'
                OR stage_name LIKE 'S10_%'
                OR stage_name LIKE 'S11_%'
                OR stage_name LIKE 'S12_%'
                OR stage_name LIKE 'S13_%'
                OR stage_name LIKE 'S14_%'
                OR stage_name LIKE 'S15_%'
                OR stage_name LIKE 'S16_%'
                OR stage_name LIKE 'S17_%'
                OR stage_name LIKE 'S18_%'
                OR stage_name LIKE 'S19_%'
                OR stage_name LIKE 'S20_%'
                OR stage_name LIKE 'S21_%'
                OR stage_name LIKE '%M253%'
        """))
        s04_stage = rows(
            con,
            """
            SELECT stage_run_id,stage_name,status,output_count,warning_count,error_count,
                   started_at,completed_at,error_message
              FROM research_backfill_stage_runs
             WHERE stage_name='S04_m251b_macro_event_backfill'
             ORDER BY stage_run_id DESC
             LIMIT 1
            """,
        )

    hard_failures: list[str] = []
    warnings: list[str] = []
    if integrity != "ok":
        hard_failures.append(f"DB integrity_check is {integrity}")
    if counts.get("macro_observations", 0) <= 0:
        hard_failures.append("macro_observations is zero")
    if any(v != 0 for v in pit.values()):
        hard_failures.append("point-in-time timestamp issues are present")
    if any(v != 0 for v in duplicates.values()):
        hard_failures.append("duplicate macro event/observation/central bank keys are present")
    if fake_macro_news != 0:
        hard_failures.append("macro source rows were inserted into raw_news_articles")
    if s05plus_count != 0:
        hard_failures.append("S05/M253+ stage rows exist")
    if any(missing_schema.values()):
        hard_failures.append("required closeout schema fields are missing")
    if non_event_cluster_link_total > 0 or energy_price_cluster_links > 0:
        hard_failures.append("regime-feature market observations are linked as event clusters")
    if central_bank_count <= 0:
        hard_failures.append("central_bank_events coverage is zero")
    if strict_replay_col_exists and strict_replay_unsafe:
        hard_failures.append("revised_latest_only/unknown vintage rows are strict replay eligible")
    if not strict_replay_col_exists:
        warnings.append("strict_replay_eligible column is missing; strict vintage replay safety cannot be fully enforced")
    if not surprise_basis_col_exists or not consensus_col_exists:
        warnings.append("surprise_basis/is_consensus_surprise columns are missing; consensus surprise safety cannot be fully enforced")
    if forecast_null_surprise_raw > 0:
        warnings.append("forecast_value NULL with surprise_raw non-null requires surprise_basis tagging")

    return {
        "database_path": str(db_path),
        "generated_at": utc_now(),
        "integrity_check": integrity,
        "counts": counts,
        "s04_stage": s04_stage[0] if s04_stage else {},
        "s05_m253plus_stage_rows": s05plus_count,
        "releases_by_source_event": releases_by_source_event,
        "non_event_cluster_links": {
            "total": non_event_cluster_link_total,
            "by_event_type": non_event_cluster_links,
            "sample_rows": sample_non_event_cluster_links,
        },
        "energy_inventory_or_price_audit": {
            "by_release_name": energy_mix,
            "energy_price_or_mixed_cluster_links": energy_price_cluster_links,
            "energy_inventory_cluster_links": energy_inventory_cluster_links,
            "sample_price_or_mixed_rows": energy_mixed_samples,
        },
        "point_in_time": pit,
        "duplicates": duplicates,
        "fake_macro_news_rows": fake_macro_news,
        "vintage": {
            "distribution": vintage_dist,
            "strict_replay_eligible_column_exists": strict_replay_col_exists,
            "strict_replay_unsafe_rows": strict_replay_unsafe,
        },
        "surprise": {
            "surprise_basis_column_exists": surprise_basis_col_exists,
            "is_consensus_surprise_column_exists": consensus_col_exists,
            "actual_minus_forecast_column_exists": actual_minus_forecast_col_exists,
            "distribution": surprise_dist,
            "bad_consensus_surprise_rows": bad_consensus_surprise,
            "bad_actual_minus_forecast_rows": bad_actual_minus_forecast,
            "forecast_null_surprise_raw_rows": forecast_null_surprise_raw,
            "sample_forecast_null_surprise_rows": sample_forecast_null_surprise,
        },
        "central_bank": {
            "count": central_bank_count,
            "distribution": central_bank_dist,
            "coverage_gap": central_bank_count == 0,
        },
        "required_schema_missing": missing_schema,
        "hard_failures": hard_failures,
        "warnings": warnings,
        "execution_pass": bool(s04_stage and s04_stage[0].get("status") == "completed" and int(s04_stage[0].get("error_count") or 0) == 0),
        "closeout_pass_without_idempotency": not hard_failures,
    }


def make_clone_config(config_path: Path, clone_db: Path, output_dir: Path) -> Path:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.setdefault("run", {})
    config["run"]["database_path"] = str(clone_db)
    config["run"]["output_dir"] = str(output_dir)
    config["run"]["resume"] = True
    config_path_out = output_dir / "s04_clone_idempotency_config.json"
    config_path_out.parent.mkdir(parents=True, exist_ok=True)
    config_path_out.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return config_path_out


def run_clone_idempotency(
    source_db: Path,
    config_path: Path,
    out_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    clone_dir = out_dir / f"s04_clone_idempotency_{ts}"
    clone_dir.mkdir(parents=True, exist_ok=True)
    clone_db = clone_dir / "canonical_market_events.clone.db"
    shutil.copy2(source_db, clone_db)
    runner_output_dir = clone_dir / "runner_artifacts"
    clone_config = make_clone_config(config_path, clone_db, runner_output_dir)

    with connect_rw(clone_db) as con:
        before = snapshot_counts(con)
    cmd = [
        sys.executable,
        "-m",
        "recorder.research_backfill_runner",
        "--config",
        str(clone_config),
        "--stage",
        "S04_m251b_macro_event_backfill",
        "--resume",
        "--force-stage",
    ]
    started_at = utc_now()
    proc = subprocess.run(
        cmd,
        cwd=str(REPO_ROOT),
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
    )
    completed_at = utc_now()
    with connect_rw(clone_db) as con:
        after = snapshot_counts(con)
        stage_rows = rows(
            con,
            """
            SELECT stage_run_id,stage_name,status,output_count,warning_count,error_count,
                   started_at,completed_at,error_message
              FROM research_backfill_stage_runs
             WHERE stage_name='S04_m251b_macro_event_backfill'
             ORDER BY stage_run_id DESC
             LIMIT 3
            """,
        )
        duplicate_checks = {
            "macro_event_key_duplicate_groups": int(one(con, """
                SELECT COUNT(*) FROM (
                    SELECT macro_event_key,COUNT(*) AS cnt
                      FROM macro_event_releases
                     WHERE COALESCE(macro_event_key,'')<>''
                     GROUP BY macro_event_key
                    HAVING COUNT(*)>1
                )
            """)),
            "macro_observation_key_duplicate_groups": int(one(con, """
                SELECT COUNT(*) FROM (
                    SELECT macro_observation_key,COUNT(*) AS cnt
                      FROM macro_observations
                     WHERE COALESCE(macro_observation_key,'')<>''
                     GROUP BY macro_observation_key
                    HAVING COUNT(*)>1
                )
            """)),
            "central_bank_key_duplicate_groups": int(one(con, """
                SELECT COUNT(*) FROM (
                    SELECT central_bank,decision_type,macro_event_id,COUNT(*) AS cnt
                      FROM central_bank_events
                     GROUP BY central_bank,decision_type,macro_event_id
                    HAVING COUNT(*)>1
                )
            """)),
        }

    diffs = {table: int(after.get(table, -1)) - int(before.get(table, -1)) for table in CANONICAL_COUNT_TABLES}
    canonical_diffs = {table: diffs.get(table) for table in S04_CANONICAL_COUNT_TABLES}
    pass_counts = all(int(v or 0) == 0 for v in canonical_diffs.values())
    pass_dups = all(int(v or 0) == 0 for v in duplicate_checks.values())
    pass_returncode = proc.returncode == 0
    return {
        "mode": "clone_resume_force_stage_to_bypass_completed_stage_skip",
        "clone_db": str(clone_db),
        "clone_config": str(clone_config),
        "command": cmd,
        "started_at": started_at,
        "completed_at": completed_at,
        "timeout_seconds": timeout_seconds,
        "returncode": proc.returncode,
        "stdout_tail": limited_tail(proc.stdout),
        "stderr_tail": limited_tail(proc.stderr),
        "before_counts": before,
        "after_counts": after,
        "diffs": diffs,
        "canonical_s04_diffs": canonical_diffs,
        "duplicate_checks": duplicate_checks,
        "stage_rows": stage_rows,
        "pass": bool(pass_returncode and pass_counts and pass_dups),
        "pass_returncode": pass_returncode,
        "pass_count_diff": pass_counts,
        "pass_duplicate_checks": pass_dups,
    }


def write_markdown(report: dict[str, Any], path: Path) -> None:
    audit = report["audit"]
    idem = report.get("clone_idempotency")
    lines: list[str] = []
    lines.append("# S04 Macro Closeout Audit")
    lines.append("")
    lines.append(f"- Generated at: `{report['generated_at']}`")
    lines.append(f"- Database: `{audit['database_path']}`")
    lines.append(f"- Execution pass: `{audit['execution_pass']}`")
    lines.append(f"- Closeout verdict: `{report['verdict']}`")
    lines.append(f"- S05/M253+ stage rows: `{audit['s05_m253plus_stage_rows']}`")
    lines.append("")
    lines.append("## Counts")
    lines.append("")
    lines.append("| table | count |")
    lines.append("|---|---:|")
    for table, count in audit["counts"].items():
        lines.append(f"| `{table}` | {count} |")
    lines.append("")
    lines.append("## Event type cluster eligibility")
    lines.append("")
    lines.append("| source | event_type | releases | cluster_links | distinct_clusters | event_cluster_eligible | reaction_trigger_eligible | regime_feature_eligible |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|")
    for row in audit["releases_by_source_event"]:
        lines.append(
            f"| {row.get('source_system')} | {row.get('event_type')} | {row.get('releases')} | {row.get('cluster_links')} | {row.get('distinct_clusters')} | {row.get('event_cluster_eligible')} | {row.get('reaction_trigger_eligible')} | {row.get('regime_feature_eligible')} |"
        )
    lines.append("")
    lines.append("## Key checks")
    lines.append("")
    lines.append(f"- Non-event cluster links: `{audit['non_event_cluster_links']['total']}`")
    lines.append(f"- Energy price/mixed cluster links: `{audit['energy_inventory_or_price_audit']['energy_price_or_mixed_cluster_links']}`")
    lines.append(f"- Central bank events: `{audit['central_bank']['count']}`")
    lines.append(f"- Fake macro news rows: `{audit['fake_macro_news_rows']}`")
    lines.append(f"- Point-in-time issues: `{sum(audit['point_in_time'].values())}`")
    lines.append(f"- Duplicate groups: `{sum(audit['duplicates'].values())}`")
    lines.append(f"- Forecast NULL + surprise_raw rows: `{audit['surprise']['forecast_null_surprise_raw_rows']}`")
    lines.append("")
    lines.append("## Vintage")
    lines.append("")
    lines.append("| vintage_quality | count |")
    lines.append("|---|---:|")
    for row in audit["vintage"]["distribution"]:
        lines.append(f"| {row.get('vintage_quality')} | {row.get('cnt')} |")
    lines.append("")
    lines.append("## Missing closeout schema")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(audit["required_schema_missing"], ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    if idem:
        lines.append("## Clone idempotency")
        lines.append("")
        lines.append(f"- Pass: `{idem['pass']}`")
        lines.append(f"- Return code: `{idem['returncode']}`")
        lines.append(f"- Clone DB: `{idem['clone_db']}`")
        lines.append("")
        lines.append("| table | diff |")
        lines.append("|---|---:|")
        for table, diff in idem["canonical_s04_diffs"].items():
            lines.append(f"| `{table}` | {diff} |")
        lines.append("")
    lines.append("## Hard failures")
    lines.append("")
    report_failures = report.get("hard_failures") or audit["hard_failures"]
    if report_failures:
        for item in report_failures:
            lines.append(f"- {item}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Warnings")
    lines.append("")
    if audit["warnings"]:
        for item in audit["warnings"]:
            lines.append(f"- {item}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Recommended next action")
    lines.append("")
    lines.append(report["recommended_next_action"])
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit S04 macro event lineage closeout without mutating production DB.")
    parser.add_argument("--database", default=str(DEFAULT_DB))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--run-clone-idempotency", action="store_true")
    parser.add_argument("--clone-timeout-seconds", type=int, default=3600)
    args = parser.parse_args()

    db_path = Path(args.database).resolve()
    config_path = Path(args.config).resolve()
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_at = utc_now()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    audit = audit_original(db_path)
    clone_result = None
    if args.run_clone_idempotency:
        clone_result = run_clone_idempotency(db_path, config_path, out_dir, args.clone_timeout_seconds)

    hard_failures = list(audit["hard_failures"])
    if clone_result is None:
        hard_failures.append("clone idempotency proof was not run")
    elif not clone_result.get("pass"):
        hard_failures.append("clone idempotency proof failed")

    if hard_failures:
        verdict = "FAIL"
    else:
        verdict = "PASS"

    recommended_next_action = (
        "Keep S05/M253+ stopped. Repair S04 macro eligibility metadata before full canonical closeout: "
        "add/backfill reaction_trigger_eligible/event_cluster_eligible/regime_feature_eligible, "
        "add/backfill strict_replay_eligible/surprise_basis/is_consensus_surprise, disable or demote "
        "non-event rates/fx/policy/price observation clusters from the reaction-trigger path, and implement "
        "S04B central bank coverage before full canonical M253."
        if verdict == "FAIL"
        else "S04 closeout checks passed; review central bank and eligibility policy before authorizing S05/M253+."
    )

    report = {
        "generated_at": generated_at,
        "verdict": verdict,
        "audit": audit,
        "clone_idempotency": clone_result,
        "hard_failures": hard_failures,
        "recommended_next_action": recommended_next_action,
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "live_threshold_promotion": False,
            "production_db_mutated": False,
        },
    }
    json_path = out_dir / f"s04_macro_closeout_audit_{stamp}.json"
    md_path = out_dir / f"s04_macro_closeout_audit_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    write_markdown(report, md_path)
    print(json.dumps({"verdict": verdict, "json": str(json_path), "md": str(md_path)}, ensure_ascii=False, indent=2))
    return 0 if verdict == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
