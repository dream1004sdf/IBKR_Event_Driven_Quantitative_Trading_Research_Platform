from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from recorder.s05_proxy_semantic_v5 import (
    ChannelEvidence,
    V5PolicyConfig,
    _PROXY_REGISTRY,
    canonical_hash,
    rank_proxy_candidates_v5,
)


VERSION = "s05p4-v5.4.1-pit-primary-proxy-ranker-research-v1"
NEW_YORK = ZoneInfo("America/New_York")
OUTPUT_TABLES = (
    "s05p4_v5_4_1_pit_proxy_runs",
    "s05p4_v5_4_1_pit_proxy_feature_snapshots",
    "s05p4_v5_4_1_pit_proxy_features",
    "s05p4_v5_4_1_pit_proxy_rankings",
    "s05p4_v5_4_1_pit_proxy_decisions",
)
FORBIDDEN_FEATURE_TOKENS = (
    "outcome",
    "return",
    "mfe",
    "mae",
    "pnl",
    "post_event",
    "future",
    "label",
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"missing CSV header: {path}")
        return list(reader)


def sqlite_snapshot(source: Path, destination: Path) -> None:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite clone: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.unlink(missing_ok=True)
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    destination_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(destination_connection)
    finally:
        destination_connection.close()
        source_connection.close()
    temporary.replace(destination)


def parse_aware_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp must include timezone: {value}")
    return parsed.astimezone(UTC)


def clip(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 6)


def log_score(value: float, floor: float, ceiling: float) -> float:
    if value <= 0.0 or floor <= 0.0 or ceiling <= floor:
        return 0.0
    return clip((math.log10(value) - math.log10(floor)) / (math.log10(ceiling) - math.log10(floor)))


def market_session_overlap(event_time: datetime) -> float:
    local = event_time.astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return 0.2
    minutes = local.hour * 60 + local.minute
    if 9 * 60 + 30 <= minutes <= 16 * 60:
        return 1.0
    if 4 * 60 <= minutes <= 20 * 60:
        return 0.6
    return 0.2


def mechanism_direction(event_direction: str) -> str:
    return {
        "tightening": "tightening",
        "easing": "easing",
        "escalation": "increase",
        "deescalation": "decrease",
        "demand_outlook_up": "increase",
        "demand_outlook_down": "decrease",
        "positive": "increase",
        "negative": "decrease",
        "mixed": "mixed",
    }.get(event_direction, "unclear")


def feature_metrics_for_symbol(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    event_time: datetime,
    feature_policy: Mapping[str, Any],
) -> dict[str, Any]:
    event_session_date = event_time.astimezone(NEW_YORK).date()
    lookback = int(feature_policy["lookback_completed_sessions"])
    minimum = int(feature_policy["minimum_completed_sessions"])
    maximum_age = int(feature_policy["maximum_last_session_age_calendar_days"])
    rows = connection.execute(
        """
        SELECT session_date, open, high, low, close, volume, trade_count, bar_time_utc
        FROM ibkr_historical_bars
        WHERE symbol = ?
          AND data_kind = 'daily'
          AND bar_size = '1 day'
          AND what_to_show = 'TRADES'
          AND use_rth = 1
          AND session_date < ?
        ORDER BY session_date DESC
        LIMIT ?
        """,
        (symbol, event_session_date.isoformat(), lookback),
    ).fetchall()
    valid = [
        row
        for row in rows
        if row[1] is not None
        and row[2] is not None
        and row[3] is not None
        and row[4] is not None
        and float(row[4]) > 0.0
        and row[5] is not None
        and float(row[5]) >= 0.0
    ]
    if not valid:
        return {
            "symbol": symbol,
            "complete": False,
            "missing_reason": "no_prior_completed_daily_trades",
            "observed_sessions": 0,
            "event_session_date": event_session_date.isoformat(),
            "max_source_session_date": "",
            "metrics": {},
        }
    ordered = list(reversed(valid))
    max_source_session_date = max(str(row[0]) for row in ordered)
    age_days = (event_session_date - datetime.fromisoformat(max_source_session_date).date()).days
    dollar_volumes = [float(row[4]) * float(row[5]) for row in ordered]
    ranges = [max(0.0, (float(row[2]) - float(row[3])) / float(row[4])) for row in ordered]
    trade_counts = [float(row[6] or 0.0) for row in ordered]
    zero_ranges = sum(1 for value in ranges if value <= 1e-12)
    same_closes = sum(
        1
        for previous, current in zip(ordered, ordered[1:])
        if abs(float(current[4]) - float(previous[4])) <= 1e-12
    )
    stale_denominator = max(1, len(ordered) - 1)
    stale_share = max(zero_ranges / len(ordered), same_closes / stale_denominator)
    median_dollar_volume = statistics.median(dollar_volumes)
    median_daily_range_pct = statistics.median(ranges)
    median_trade_count = statistics.median(trade_counts)
    observed = len(ordered)
    complete = observed >= minimum and age_days <= maximum_age
    missing_reason = ""
    if observed < minimum:
        missing_reason = "insufficient_prior_completed_sessions"
    elif age_days > maximum_age:
        missing_reason = "stale_prior_completed_session"
    metrics = {
        "median_dollar_volume_score": log_score(
            median_dollar_volume,
            float(feature_policy["dollar_volume_floor"]),
            float(feature_policy["dollar_volume_ceiling"]),
        ),
        "spread_quality": clip(
            1.0 - median_daily_range_pct / float(feature_policy["daily_high_low_spread_ceiling"])
        ),
        "intraday_trade_coverage": log_score(
            median_trade_count,
            float(feature_policy["daily_trade_count_floor"]),
            float(feature_policy["daily_trade_count_ceiling"]),
        ),
        "stale_trade_quality": clip(1.0 - stale_share),
        "market_session_overlap": market_session_overlap(event_time),
        "family_exposure": clip(float(feature_policy["family_exposure_neutral_prior"])),
        "beta_purity": clip(float(feature_policy["beta_purity_neutral_prior"])),
        "data_availability": clip((observed / lookback) * max(0.0, 1.0 - max(0, age_days - 3) / maximum_age)),
    }
    return {
        "symbol": symbol,
        "complete": complete,
        "missing_reason": missing_reason,
        "observed_sessions": observed,
        "event_session_date": event_session_date.isoformat(),
        "min_source_session_date": min(str(row[0]) for row in ordered),
        "max_source_session_date": max_source_session_date,
        "last_session_age_calendar_days": age_days,
        "median_dollar_volume": round(median_dollar_volume, 6),
        "median_daily_high_low_pct": round(median_daily_range_pct, 8),
        "median_daily_trade_count": round(median_trade_count, 6),
        "zero_range_share": round(zero_ranges / len(ordered), 8),
        "unchanged_close_share": round(same_closes / stale_denominator, 8),
        "metrics": metrics,
    }


def load_semantic_rows(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT s.review_row_id, s.event_cluster_id, s.direction, s.affected_channel,
               s.proxy_family, a.resolution_status,
               COALESCE(g.primary_channel, s.affected_channel) AS primary_channel
        FROM s05p4_v5_4_1_provisional_semantic_rows AS s
        JOIN s05p4_v5_4_1_provisional_target_adjudications AS a
          ON a.review_row_id = s.review_row_id
         AND a.field_name = 'proxy_family'
        LEFT JOIN s05p4_v5_4_1_provisional_region_channel_graphs AS g
          ON g.review_row_id = s.review_row_id
        ORDER BY s.review_row_id
        """
    ).fetchall()
    if len(rows) != 400:
        raise RuntimeError(f"expected 400 provisional semantic rows, got {len(rows)}")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row[0] in result:
            raise RuntimeError(f"duplicate semantic review_row_id: {row[0]}")
        result[str(row[0])] = {
            "event_cluster_id": int(row[1]),
            "direction": str(row[2]),
            "affected_channel": str(row[3]),
            "proxy_family": str(row[4]),
            "family_resolution_status": str(row[5]),
            "primary_channel": str(row[6]),
        }
    return result


def build_ranker_rows(
    *,
    safe_input_rows: Sequence[Mapping[str, str]],
    semantic_rows: Mapping[str, Mapping[str, Any]],
    cache_connection: sqlite3.Connection,
    ranker_config: Mapping[str, Any],
    semantic_policy: V5PolicyConfig,
) -> dict[str, Any]:
    feature_policy = ranker_config["feature_policy"]
    adjudication_policy = ranker_config["adjudication_policy"]
    eligible_prefix = str(adjudication_policy["eligible_family_resolution_prefix"])
    family_confidence = float(adjudication_policy["resolved_family_confidence"])
    decisions: list[dict[str, Any]] = []
    snapshots: list[dict[str, Any]] = []
    features: list[dict[str, Any]] = []
    rankings: list[dict[str, Any]] = []

    for source in safe_input_rows:
        review_row_id = source["review_row_id"]
        if review_row_id not in semantic_rows:
            raise RuntimeError(f"safe Set A row missing provisional semantic row: {review_row_id}")
        semantic = semantic_rows[review_row_id]
        event_time = parse_aware_utc(source["plan_as_of_utc"])
        family = str(semantic["proxy_family"])
        resolution_status = str(semantic["family_resolution_status"])
        specs = _PROXY_REGISTRY.get(family, ())
        candidate_symbols = [spec.symbol for spec in specs]
        raw_features: list[dict[str, Any]] = []
        metrics: dict[str, dict[str, float]] = {}
        if resolution_status.startswith(eligible_prefix) and specs:
            for symbol in candidate_symbols:
                row = feature_metrics_for_symbol(
                    cache_connection,
                    symbol=symbol,
                    event_time=event_time,
                    feature_policy=feature_policy,
                )
                raw_features.append(row)
                if row["complete"]:
                    metrics[symbol] = dict(row["metrics"])
        feature_payload = {
            "version": VERSION,
            "review_row_id": review_row_id,
            "event_cluster_id": semantic["event_cluster_id"],
            "plan_as_of_utc": event_time.isoformat(),
            "event_session_date": event_time.astimezone(NEW_YORK).date().isoformat(),
            "proxy_family": family,
            "family_resolution_status": resolution_status,
            "candidate_symbols": candidate_symbols,
            "feature_policy": feature_policy,
            "features": raw_features,
        }
        serialized_feature_payload = json.dumps(
            feature_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if any(token in serialized_feature_payload.lower() for token in FORBIDDEN_FEATURE_TOKENS):
            raise RuntimeError(f"forbidden post-event token in feature payload: {review_row_id}")
        snapshot_id = canonical_hash(feature_payload)
        missing_symbols = [row["symbol"] for row in raw_features if not row["complete"]]
        all_candidates_complete = bool(specs) and len(metrics) == len(specs)
        max_source_session_date = max(
            (str(row.get("max_source_session_date", "")) for row in raw_features),
            default="",
        )
        pit_violation_count = sum(
            1
            for row in raw_features
            if row.get("max_source_session_date", "")
            and str(row["max_source_session_date"]) >= str(row["event_session_date"])
        )
        snapshots.append({
            "snapshot_id": snapshot_id,
            "review_row_id": review_row_id,
            "event_cluster_id": semantic["event_cluster_id"],
            "plan_as_of_utc": event_time.isoformat(),
            "event_session_date": event_time.astimezone(NEW_YORK).date().isoformat(),
            "proxy_family": family,
            "expected_candidate_count": len(candidate_symbols),
            "complete_candidate_count": len(metrics),
            "all_candidates_complete": int(all_candidates_complete),
            "max_source_session_date": max_source_session_date,
            "pit_violation_count": pit_violation_count,
            "feature_payload_json": serialized_feature_payload,
        })
        for row in raw_features:
            features.append({
                "feature_key": canonical_hash([snapshot_id, row["symbol"]]),
                "snapshot_id": snapshot_id,
                "review_row_id": review_row_id,
                "symbol": row["symbol"],
                "complete": int(bool(row["complete"])),
                "missing_reason": row["missing_reason"],
                "observed_sessions": int(row["observed_sessions"]),
                "min_source_session_date": row.get("min_source_session_date", ""),
                "max_source_session_date": row.get("max_source_session_date", ""),
                "last_session_age_calendar_days": row.get("last_session_age_calendar_days"),
                "raw_statistics_json": json.dumps(
                    {key: value for key, value in row.items() if key != "metrics"},
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "metrics_json": json.dumps(
                    row.get("metrics", {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
            })

        selected = ""
        raw_selected = ""
        confidence = 0.0
        gap = 0.0
        ranker_abstain_reason = ""
        final_abstain_reason = ""
        ranked_rows: Sequence[Any] = ()
        if not resolution_status.startswith(eligible_prefix):
            final_abstain_reason = (
                "proxy_family_explicit_abstain"
                if resolution_status == "explicit_abstain"
                else "proxy_family_not_independently_resolved"
            )
        elif not specs:
            final_abstain_reason = "no_proxy_registry_for_resolved_family"
        else:
            primary_channel = str(semantic["primary_channel"] or "unknown")
            channels = (
                ChannelEvidence(
                    primary_channel,
                    "primary",
                    mechanism_direction(str(semantic["direction"])),
                    1.0,
                    "owner-accepted provisional Set A region/channel graph",
                    "s05p4-v5.4.1-provisional-graph",
                ),
            )
            ranked_rows, raw_selected, confidence, gap, ranker_abstained, ranker_abstain_reason = (
                rank_proxy_candidates_v5(
                    family,
                    family_confidence,
                    str(semantic["direction"]),
                    (),
                    channels,
                    pre_event_metrics=metrics,
                    pre_event_feature_snapshot_id=snapshot_id,
                    thresholds=semantic_policy.thresholds,
                )
            )
            if not all_candidates_complete:
                final_abstain_reason = "incomplete_candidate_market_data:" + ",".join(missing_symbols)
            elif ranker_abstained:
                final_abstain_reason = ranker_abstain_reason
            elif (
                bool(ranker_config["selection_policy"]["enforce_v5_primary_proxy_confidence_threshold"])
                and confidence < semantic_policy.thresholds.primary_proxy
            ):
                final_abstain_reason = "primary_proxy_confidence_below_minimum"
            else:
                selected = raw_selected
            for candidate in ranked_rows:
                rankings.append({
                    "candidate_key": canonical_hash([snapshot_id, candidate.symbol]),
                    "snapshot_id": snapshot_id,
                    "review_row_id": review_row_id,
                    "event_cluster_id": semantic["event_cluster_id"],
                    "proxy_family": family,
                    "symbol": candidate.symbol,
                    "rank": candidate.rank,
                    "final_score": candidate.final_score,
                    "expected_direction": candidate.expected_direction,
                    "expected_direction_confidence": candidate.expected_direction_confidence,
                    "candidate_json": json.dumps(
                        asdict(candidate), ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    ),
                })
        decisions.append({
            "decision_key": canonical_hash([VERSION, review_row_id, snapshot_id]),
            "review_row_id": review_row_id,
            "event_cluster_id": semantic["event_cluster_id"],
            "headline": source["normalized_headline"],
            "provider": source["provider"],
            "plan_as_of_utc": event_time.isoformat(),
            "proxy_family": family,
            "family_resolution_status": resolution_status,
            "candidate_symbols_json": json.dumps(candidate_symbols, separators=(",", ":")),
            "feature_snapshot_id": snapshot_id,
            "raw_top_symbol": raw_selected,
            "primary_proxy_selected": selected,
            "primary_proxy_confidence": confidence,
            "top1_top2_gap": gap,
            "abstained": int(not bool(selected)),
            "ranker_abstain_reason": ranker_abstain_reason,
            "final_abstain_reason": final_abstain_reason,
            "all_candidates_complete": int(all_candidates_complete),
        })
    return {
        "decisions": decisions,
        "snapshots": snapshots,
        "features": features,
        "rankings": rankings,
    }


def create_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_pit_proxy_runs (
            run_key TEXT PRIMARY KEY,
            version TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            source_clone_sha256 TEXT NOT NULL,
            cache_sha256 TEXT NOT NULL,
            canonical_sha256 TEXT NOT NULL,
            set_a_rows INTEGER NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_pit_proxy_feature_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            run_key TEXT NOT NULL,
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            plan_as_of_utc TEXT NOT NULL,
            event_session_date TEXT NOT NULL,
            proxy_family TEXT NOT NULL,
            expected_candidate_count INTEGER NOT NULL,
            complete_candidate_count INTEGER NOT NULL,
            all_candidates_complete INTEGER NOT NULL,
            max_source_session_date TEXT NOT NULL,
            pit_violation_count INTEGER NOT NULL,
            feature_payload_json TEXT NOT NULL,
            UNIQUE(run_key, review_row_id)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_pit_proxy_features (
            feature_key TEXT PRIMARY KEY,
            run_key TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            review_row_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            complete INTEGER NOT NULL,
            missing_reason TEXT NOT NULL,
            observed_sessions INTEGER NOT NULL,
            min_source_session_date TEXT NOT NULL,
            max_source_session_date TEXT NOT NULL,
            last_session_age_calendar_days INTEGER,
            raw_statistics_json TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            UNIQUE(run_key, review_row_id, symbol)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_pit_proxy_rankings (
            candidate_key TEXT PRIMARY KEY,
            run_key TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            proxy_family TEXT NOT NULL,
            symbol TEXT NOT NULL,
            rank INTEGER NOT NULL,
            final_score REAL NOT NULL,
            expected_direction TEXT NOT NULL,
            expected_direction_confidence REAL NOT NULL,
            candidate_json TEXT NOT NULL,
            UNIQUE(run_key, review_row_id, symbol),
            UNIQUE(run_key, review_row_id, rank)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_4_1_pit_proxy_decisions (
            decision_key TEXT PRIMARY KEY,
            run_key TEXT NOT NULL,
            review_row_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            headline TEXT NOT NULL,
            provider TEXT NOT NULL,
            plan_as_of_utc TEXT NOT NULL,
            proxy_family TEXT NOT NULL,
            family_resolution_status TEXT NOT NULL,
            candidate_symbols_json TEXT NOT NULL,
            feature_snapshot_id TEXT NOT NULL,
            raw_top_symbol TEXT NOT NULL,
            primary_proxy_selected TEXT NOT NULL,
            primary_proxy_confidence REAL NOT NULL,
            top1_top2_gap REAL NOT NULL,
            abstained INTEGER NOT NULL,
            ranker_abstain_reason TEXT NOT NULL,
            final_abstain_reason TEXT NOT NULL,
            all_candidates_complete INTEGER NOT NULL,
            UNIQUE(run_key, review_row_id)
        );
        """
    )


def insert_rows(connection: sqlite3.Connection, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    sql = (
        f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})"
    )
    connection.executemany(sql, [tuple(row[column] for column in columns) for row in rows])


def persist_payload(
    connection: sqlite3.Connection,
    *,
    run_row: Mapping[str, Any],
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
) -> None:
    create_tables(connection)
    columns = list(run_row)
    connection.execute(
        f"INSERT OR IGNORE INTO {OUTPUT_TABLES[0]} ({','.join(columns)}) "
        f"VALUES ({','.join('?' for _ in columns)})",
        tuple(run_row[column] for column in columns),
    )
    mapping = {
        OUTPUT_TABLES[1]: payload["snapshots"],
        OUTPUT_TABLES[2]: payload["features"],
        OUTPUT_TABLES[3]: payload["rankings"],
        OUTPUT_TABLES[4]: payload["decisions"],
    }
    for table, source_rows in mapping.items():
        rows = [{"run_key": run_row["run_key"], **dict(row)} for row in source_rows]
        insert_rows(connection, table, rows)
    connection.commit()


def table_state(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for table in OUTPUT_TABLES:
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        state[table] = {"count": len(rows), "payload_hash": canonical_hash(rows)}
    return state


def distribution(values: Iterable[str]) -> dict[str, int]:
    return dict(sorted(Counter(values).items()))


def write_decision_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0]) if rows else []
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def build_report(
    *,
    args: argparse.Namespace,
    run_key: str,
    config_hash: str,
    input_hash: str,
    payload: Mapping[str, Sequence[Mapping[str, Any]]],
    preexisting_state: Mapping[str, Any] | None,
    first_state: Mapping[str, Any],
    second_state: Mapping[str, Any],
    checksums: Mapping[str, str],
    cache_coverage: Mapping[str, Any],
    quick_check: str,
) -> dict[str, Any]:
    decisions = list(payload["decisions"])
    selected = [row for row in decisions if row["primary_proxy_selected"]]
    abstained = [row for row in decisions if not row["primary_proxy_selected"]]
    pit_violations = sum(int(row["pit_violation_count"]) for row in payload["snapshots"])
    duplicate_counts = {
        "decision_key": len(decisions) - len({row["decision_key"] for row in decisions}),
        "review_row_id": len(decisions) - len({row["review_row_id"] for row in decisions}),
        "snapshot_id": len(payload["snapshots"]) - len({row["snapshot_id"] for row in payload["snapshots"]}),
        "feature_key": len(payload["features"]) - len({row["feature_key"] for row in payload["features"]}),
        "candidate_key": len(payload["rankings"]) - len({row["candidate_key"] for row in payload["rankings"]}),
    }
    rerun_diff = {
        table: {
            "count_diff": second_state[table]["count"] - first_state[table]["count"],
            "payload_hash_unchanged": second_state[table]["payload_hash"] == first_state[table]["payload_hash"],
        }
        for table in OUTPUT_TABLES
    }
    resume_diff = None
    if preexisting_state is not None:
        resume_diff = {
            table: {
                "count_diff": second_state[table]["count"] - preexisting_state[table]["count"],
                "payload_hash_unchanged": second_state[table]["payload_hash"]
                == preexisting_state[table]["payload_hash"],
            }
            for table in OUTPUT_TABLES
        }
    hard_gates = {
        "set_a_rows_exactly_400": len(decisions) == 400,
        "set_b_rows_used_zero": True,
        "canonical_checksum_unchanged": checksums["canonical_before"] == checksums["canonical_after"],
        "source_clone_checksum_unchanged": checksums["source_clone_before"] == checksums["source_clone_after"],
        "cache_checksum_unchanged": checksums["cache_before"] == checksums["cache_after"],
        "pit_critical_issues_zero": pit_violations == 0,
        "duplicate_keys_zero": all(value == 0 for value in duplicate_counts.values()),
        "same_input_rerun_diff_zero": all(
            row["count_diff"] == 0 and row["payload_hash_unchanged"] for row in rerun_diff.values()
        ),
        "cross_process_resume_diff_zero": resume_diff is None
        or all(
            row["count_diff"] == 0 and row["payload_hash_unchanged"]
            for row in resume_diff.values()
        ),
        "quick_check_ok": quick_check == "ok",
        "outcome_or_post_event_features_used_zero": True,
        "same_event_daily_bars_used_zero": pit_violations == 0,
        "forced_selection_with_incomplete_candidates_zero": not any(
            row["primary_proxy_selected"] and not row["all_candidates_complete"] for row in decisions
        ),
    }
    status = "PASS" if all(hard_gates.values()) else "FAIL"
    verdict = (
        "PASS_CLONE_ONLY_PIT_PRIMARY_PROXY_RESEARCH_APPLICATION_HOLD"
        if status == "PASS"
        else "FAIL_CLONE_ONLY_PIT_PRIMARY_PROXY_RESEARCH"
    )
    return {
        "version": VERSION,
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "status": status,
        "verdict": verdict,
        "scope": "owner-accepted provisional AI Set A only; clone-only PIT proxy ranking",
        "run_key": run_key,
        "config_hash": config_hash,
        "input_hash": input_hash,
        "paths": {
            "clone_db": str(args.output_clone.resolve()),
            "source_clone": str(args.source_clone.resolve()),
            "market_data_cache": str(args.cache_db.resolve()),
            "safe_set_a_input": str(args.safe_set_a_input.resolve()),
            "decision_csv": str(args.decision_csv.resolve()),
        },
        "counts": {
            "set_a_rows": len(decisions),
            "selected": len(selected),
            "abstained": len(abstained),
            "feature_snapshots": len(payload["snapshots"]),
            "symbol_feature_rows": len(payload["features"]),
            "candidate_ranking_rows": len(payload["rankings"]),
            "complete_family_candidate_sets": sum(int(row["all_candidates_complete"]) for row in decisions),
        },
        "distributions": {
            "selected_symbol": distribution(str(row["primary_proxy_selected"]) for row in selected),
            "selected_family": distribution(str(row["proxy_family"]) for row in selected),
            "family_resolution_status": distribution(str(row["family_resolution_status"]) for row in decisions),
            "final_abstain_reason": distribution(str(row["final_abstain_reason"]) for row in abstained),
            "proxy_family": distribution(str(row["proxy_family"]) for row in decisions),
        },
        "market_data_coverage": cache_coverage,
        "point_in_time_audit": {
            "policy": "daily TRADES RTH session_date strictly before event New York session date",
            "same_or_future_event_session_rows_used": pit_violations,
            "bid_ask_spread_claimed": False,
            "spread_feature": "OHLCV daily high-low proxy",
            "continuous_intraday_coverage_claimed": False,
            "trade_coverage_feature": "daily trade-count activity proxy",
            "neutral_family_exposure_and_beta_priors": True,
        },
        "duplicate_audit": duplicate_counts,
        "idempotency_rerun": rerun_diff,
        "cross_process_resume": {
            "preexisting_same_run_found": preexisting_state is not None,
            "diff": resume_diff or {},
        },
        "checksums": dict(checksums),
        "quick_check": quick_check,
        "hard_gates": hard_gates,
        "safety": {
            "semantic_rules_changed": False,
            "canonical_database_modified": False,
            "set_b_released": False,
            "full_range_backfill_started": False,
            "canonical_promotion": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
        "application_decision": "HOLD_PENDING_OFFICIAL_HUMAN_AUTHORITY_AND_PRIMARY_PROXY_VALIDATION",
        "limitations": [
            "This is owner-accepted provisional AI research evidence, not official independent human adjudication.",
            "The existing IBKR cache does not contain every registry candidate, so incomplete families abstain.",
            "Spread quality is an OHLCV daily range proxy, not a contemporaneous bid/ask spread.",
            "Intraday trade coverage is a daily trade-activity proxy because continuous minute coverage is unavailable.",
            "Family exposure and beta purity are neutral non-ranking priors until independent PIT estimates exist.",
        ],
        "selected_samples": selected[:20],
        "abstain_samples": abstained[:20],
    }


def markdown_report(report: Mapping[str, Any]) -> str:
    counts = report["counts"]
    lines = [
        "# S05P4 v5.4.1 clone-only PIT primary-proxy ranker",
        "",
        f"- Verdict: `{report['verdict']}`",
        f"- Set A: `{counts['set_a_rows']}`",
        f"- Selected / abstained: `{counts['selected']} / {counts['abstained']}`",
        f"- Complete family candidate sets: `{counts['complete_family_candidate_sets']}`",
        f"- Candidate ranking rows: `{counts['candidate_ranking_rows']}`",
        f"- Application: `{report['application_decision']}`",
        "",
        "## Selection distribution",
        "",
        "```json",
        json.dumps(report["distributions"]["selected_symbol"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Abstention distribution",
        "",
        "```json",
        json.dumps(report["distributions"]["final_abstain_reason"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Point-in-time audit",
        "",
        "```json",
        json.dumps(report["point_in_time_audit"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## Hard gates",
        "",
    ]
    for key, value in report["hard_gates"].items():
        lines.append(f"- {'PASS' if value else 'FAIL'} `{key}`")
    lines.extend(["", "## Limitations", ""])
    for item in report["limitations"]:
        lines.append(f"- {item}")
    lines.extend([
        "",
        "Canonical promotion, M254/M255/M256, Set B release, and broker paths remain disabled.",
        "",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--canonical-db",
        type=Path,
        default=ROOT / "work/canonical_research_backfill/m254_canonical_20250101_20260706_v1/canonical_market_events.db",
    )
    parser.add_argument(
        "--source-clone",
        type=Path,
        default=ROOT / "work/s05p4_v5_4_1_provisional_ai_dry_run/v1/canonical_market_events_s05p4_v5_4_1_provisional_ai_clone.db",
    )
    parser.add_argument(
        "--cache-db",
        type=Path,
        default=ROOT / "work/s05_full_range_news_clone/v1/ibkr_s05_full_range_cache.db",
    )
    parser.add_argument(
        "--safe-set-a-input",
        type=Path,
        default=ROOT / "artifacts/review/s05p4_independent_semantic_v1/human_review_input_A.csv",
    )
    parser.add_argument(
        "--protocol-freeze",
        type=Path,
        default=ROOT / "artifacts/review/s05p4_independent_semantic_v1/REVIEW_PROTOCOL_FREEZE.json",
    )
    parser.add_argument(
        "--ranker-config",
        type=Path,
        default=ROOT / "config/s05p4_v5_4_1_pit_primary_proxy_ranker_research.json",
    )
    parser.add_argument(
        "--semantic-config",
        type=Path,
        default=ROOT / "config/s05p4_semantic_v5_4_1_research.json",
    )
    parser.add_argument(
        "--output-clone",
        type=Path,
        default=ROOT / "work/s05p4_v5_4_1_pit_primary_proxy_ranker/v1/canonical_market_events_s05p4_v5_4_1_pit_proxy_clone.db",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        default=ROOT / "artifacts/audit/s05p4_v5_4_1_pit_primary_proxy_ranker.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=ROOT / "artifacts/audit/s05p4_v5_4_1_pit_primary_proxy_ranker.md",
    )
    parser.add_argument(
        "--decision-csv",
        type=Path,
        default=ROOT / "artifacts/audit/s05p4_v5_4_1_pit_primary_proxy_decisions.csv",
    )
    parser.add_argument("--resume", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    protocol = read_json(args.protocol_freeze)
    ranker_config = read_json(args.ranker_config)
    semantic_config = read_json(args.semantic_config)
    if ranker_config.get("version") != VERSION:
        raise RuntimeError("ranker config version mismatch")
    semantic_policy = V5PolicyConfig.from_config(semantic_config)
    safe_input_sha = sha256_file(args.safe_set_a_input)
    if safe_input_sha != protocol["checksums"]["set_a_input_sha256"]:
        raise RuntimeError("safe Set A input hash differs from frozen protocol")
    safe_rows = read_csv(args.safe_set_a_input)
    if len(safe_rows) != 400 or len({row["review_row_id"] for row in safe_rows}) != 400:
        raise RuntimeError("safe Set A input must contain 400 unique rows")

    canonical_before = sha256_file(args.canonical_db)
    if canonical_before != protocol["checksums"]["canonical_source_sha256"]:
        raise RuntimeError("canonical DB hash differs from frozen protocol")
    source_clone_before = sha256_file(args.source_clone)
    cache_before = sha256_file(args.cache_db)
    if args.output_clone.exists() and not args.resume:
        raise FileExistsError(f"output clone already exists; use --resume: {args.output_clone}")
    if not args.output_clone.exists():
        sqlite_snapshot(args.source_clone, args.output_clone)

    with sqlite3.connect(f"file:{args.source_clone.resolve()}?mode=ro", uri=True) as source_connection:
        semantic_rows = load_semantic_rows(source_connection)
    config_hash = canonical_hash(ranker_config)
    input_hash = canonical_hash({
        "safe_set_a_input_sha256": safe_input_sha,
        "source_clone_sha256": source_clone_before,
        "cache_sha256": cache_before,
        "semantic_config_sha256": sha256_file(args.semantic_config),
        "ranker_config_hash": config_hash,
    })
    run_key = canonical_hash([VERSION, input_hash])
    with sqlite3.connect(f"file:{args.cache_db.resolve()}?mode=ro", uri=True) as cache_connection:
        cache_connection.execute("PRAGMA query_only=ON")
        payload = build_ranker_rows(
            safe_input_rows=safe_rows,
            semantic_rows=semantic_rows,
            cache_connection=cache_connection,
            ranker_config=ranker_config,
            semantic_policy=semantic_policy,
        )
        registry_symbols = sorted({spec.symbol for specs in _PROXY_REGISTRY.values() for spec in specs})
        placeholders = ",".join("?" for _ in registry_symbols)
        coverage_rows = cache_connection.execute(
            f"""
            SELECT symbol, COUNT(*), MIN(session_date), MAX(session_date)
            FROM ibkr_historical_bars
            WHERE data_kind='daily' AND bar_size='1 day' AND what_to_show='TRADES'
              AND use_rth=1 AND symbol IN ({placeholders})
            GROUP BY symbol ORDER BY symbol
            """,
            registry_symbols,
        ).fetchall()
        coverage_by_symbol = {
            str(row[0]): {"daily_rows": int(row[1]), "first_session": row[2], "last_session": row[3]}
            for row in coverage_rows
        }
        cache_coverage = {
            "registry_symbol_count": len(registry_symbols),
            "registry_symbols_with_daily_trades": len(coverage_by_symbol),
            "registry_symbols_missing_daily_trades": [symbol for symbol in registry_symbols if symbol not in coverage_by_symbol],
            "by_symbol": coverage_by_symbol,
        }

    run_row = {
        "run_key": run_key,
        "version": VERSION,
        "config_hash": config_hash,
        "input_hash": input_hash,
        "source_clone_sha256": source_clone_before,
        "cache_sha256": cache_before,
        "canonical_sha256": canonical_before,
        "set_a_rows": 400,
        "created_at_utc": datetime.now(UTC).isoformat(),
    }
    with sqlite3.connect(args.output_clone) as output_connection:
        create_tables(output_connection)
        preexisting_run = output_connection.execute(
            f"SELECT COUNT(*) FROM {OUTPUT_TABLES[0]} WHERE run_key=?", (run_key,)
        ).fetchone()[0]
        preexisting_state = table_state(output_connection) if preexisting_run else None
        persist_payload(output_connection, run_row=run_row, payload=payload)
        first_state = table_state(output_connection)
        persist_payload(output_connection, run_row=run_row, payload=payload)
        second_state = table_state(output_connection)
        quick_check = str(output_connection.execute("PRAGMA quick_check").fetchone()[0])

    canonical_after = sha256_file(args.canonical_db)
    source_clone_after = sha256_file(args.source_clone)
    cache_after = sha256_file(args.cache_db)
    checksums = {
        "canonical_before": canonical_before,
        "canonical_after": canonical_after,
        "source_clone_before": source_clone_before,
        "source_clone_after": source_clone_after,
        "cache_before": cache_before,
        "cache_after": cache_after,
        "output_clone_after": sha256_file(args.output_clone),
    }
    write_decision_csv(args.decision_csv, payload["decisions"])
    report = build_report(
        args=args,
        run_key=run_key,
        config_hash=config_hash,
        input_hash=input_hash,
        payload=payload,
        preexisting_state=preexisting_state,
        first_state=first_state,
        second_state=second_state,
        checksums=checksums,
        cache_coverage=cache_coverage,
        quick_check=quick_check,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    args.markdown_output.write_text(markdown_report(report), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "verdict": report["verdict"],
        "selected": report["counts"]["selected"],
        "abstained": report["counts"]["abstained"],
        "json_output": str(args.json_output.resolve()),
        "markdown_output": str(args.markdown_output.resolve()),
    }, ensure_ascii=False))
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
