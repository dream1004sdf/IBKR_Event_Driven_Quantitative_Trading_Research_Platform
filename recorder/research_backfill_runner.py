from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from recorder.config import Settings
from recorder.macro_event_lineage import rebuild_macro_event_lineage_from_observations
from recorder.news_lineage_v2 import (
    NEWS_ASSET_MAPPER_VERSION,
    NEWS_EVENT_CLUSTERER_VERSION,
    SECURITY_MASTER_VERSION,
    ensure_news_lineage_v2_schema,
    rebuild_news_lineage_v2,
)
from recorder.reaction_research_engine import (
    EVENT_PROXY_MAP_VERSION,
    REACTION_LABEL_OUTCOME_BUILDER_VERSION,
    REACTION_RESEARCH_ENGINE_VERSION,
    build_reaction_label_foundation,
)

RESEARCH_BACKFILL_RUNNER_VERSION = "canonical-research-backfill-runner-v1"
REPLAY_FEATURE_VERSION = "replay_features_v1"
REPLAY_OUTCOME_VERSION = "replay_outcomes_v1"
DEFAULT_CANONICAL_SYMBOLS = (
    "SPY,QQQ,IWM,DIA,SMH,SOXX,NVDA,MSFT,AAPL,AMZN,META,GOOGL,TSLA,"
    "AVGO,AMD,MU,TSM,USO,XLE,XOM,CVX,GLD,UUP,TLT,ITA,XAR,EWY,CIBR,HACK,DBA,WEAT"
)
DEFAULT_IBKR_NEWS_PROVIDER_CODES = ("BZ", "DJ-N", "DJ-RT", "DJ-RTA", "DJ-RTE", "DJ-RTG", "DJNL")


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalise_utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _read_config(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_text(encoding="utf-8")
    config_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore

            payload = yaml.safe_load(raw)
        except Exception as exc:  # pragma: no cover - depends on optional yaml install
            raise RuntimeError(
                f"Failed to parse config as JSON/YAML. Use JSON-compatible YAML for portability: {path}"
            ) from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Config root must be an object: {path}")
    return payload, config_hash


def _parse_local_datetime(value: str, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    parsed = datetime.fromisoformat(str(value))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(UTC)


def _safe_name(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value).strip("_") or "stage"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str) -> int:
    if not _table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _config_value(payload: dict[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _bool_config(payload: dict[str, Any], *path: str, default: bool = False) -> bool:
    value = _config_value(payload, *path, default=default)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _int_config(payload: dict[str, Any], *path: str, default: int = 0) -> int:
    value = _config_value(payload, *path, default=default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


@dataclass(frozen=True)
class BackfillContext:
    run_id: str
    build_id: str
    config_path: Path
    config: dict[str, Any]
    config_hash: str
    database_path: Path
    start_utc: datetime
    end_utc: datetime
    timezone_name: str
    resume: bool
    force_stage: bool
    allow_blind: bool
    dry_run: bool
    output_dir: Path

    @property
    def start_iso(self) -> str:
        return _normalise_utc_iso(self.start_utc)

    @property
    def end_iso(self) -> str:
        return _normalise_utc_iso(self.end_utc)


@dataclass(frozen=True)
class Stage:
    name: str
    fn: Callable[[BackfillContext], dict[str, Any]]
    version: str = RESEARCH_BACKFILL_RUNNER_VERSION
    requires: tuple[str, ...] = ()
    audit_gate: bool = False
    blind_stage: bool = False


@dataclass(frozen=True)
class ExternalSignalConversionWorkItem:
    command_name: str
    label: str
    source: str
    external_id_prefix: str
    symbol: str
    start_date: str
    end_date: str
    estimated_rows: int
    slice_key: str = ""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_run_control_schema(conn: sqlite3.Connection) -> None:
    ensure_news_lineage_v2_schema(conn)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS research_backfill_runs (
            run_id TEXT PRIMARY KEY,
            build_id TEXT NOT NULL DEFAULT '',
            start_utc TEXT NOT NULL DEFAULT '',
            end_utc TEXT NOT NULL DEFAULT '',
            timezone TEXT NOT NULL DEFAULT '',
            config_path TEXT NOT NULL DEFAULT '',
            config_hash TEXT NOT NULL DEFAULT '',
            code_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'created',
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS research_backfill_stage_runs (
            stage_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            stage_version TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            input_watermark_start TEXT NOT NULL DEFAULT '',
            input_watermark_end TEXT NOT NULL DEFAULT '',
            output_count INTEGER NOT NULL DEFAULT 0,
            warning_count INTEGER NOT NULL DEFAULT 0,
            error_count INTEGER NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(run_id,stage_name)
        );

        CREATE TABLE IF NOT EXISTS research_backfill_stage_slices (
            slice_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            stage_name TEXT NOT NULL,
            slice_start_utc TEXT NOT NULL DEFAULT '',
            slice_end_utc TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            output_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            started_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT NOT NULL DEFAULT '',
            UNIQUE(run_id,stage_name,slice_start_utc,slice_end_utc)
        );

        CREATE TABLE IF NOT EXISTS research_build_artifacts (
            artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            artifact_type TEXT NOT NULL,
            path TEXT NOT NULL,
            row_count INTEGER NOT NULL DEFAULT 0,
            checksum TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            UNIQUE(run_id,artifact_type,path)
        );
        """
    )


def register_or_resume_run(ctx: BackfillContext) -> None:
    now = utc_now_iso()
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO research_backfill_runs(
                   run_id,build_id,start_utc,end_utc,timezone,config_path,config_hash,
                   code_version,status,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                ctx.run_id,
                ctx.build_id,
                ctx.start_iso,
                ctx.end_iso,
                ctx.timezone_name,
                str(ctx.config_path),
                ctx.config_hash,
                RESEARCH_BACKFILL_RUNNER_VERSION,
                "created",
                now,
            ),
        )
        conn.execute(
            """UPDATE research_backfill_runs
                  SET status='running',
                      started_at=CASE WHEN started_at='' THEN ? ELSE started_at END,
                      error_message=''
                WHERE run_id=?""",
            (now, ctx.run_id),
        )


def stage_completed(ctx: BackfillContext, stage_name: str) -> bool:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        row = conn.execute(
            """SELECT status
                 FROM research_backfill_stage_runs
                WHERE run_id=? AND stage_name=?""",
            (ctx.run_id, stage_name),
        ).fetchone()
        return bool(row and row["status"] == "completed")


def mark_stage_running(ctx: BackfillContext, stage: Stage) -> None:
    now = utc_now_iso()
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO research_backfill_stage_runs(
                   run_id,stage_name,stage_version,status,input_watermark_start,
                   input_watermark_end,started_at
               ) VALUES (?,?,?,?,?,?,?)""",
            (ctx.run_id, stage.name, stage.version, "pending", ctx.start_iso, ctx.end_iso, now),
        )
        conn.execute(
            """UPDATE research_backfill_stage_runs
                  SET status='running',
                      stage_version=?,
                      input_watermark_start=?,
                      input_watermark_end=?,
                      started_at=?,
                      completed_at='',
                      error_message='',
                      payload_json='{}'
                WHERE run_id=? AND stage_name=?""",
            (stage.version, ctx.start_iso, ctx.end_iso, now, ctx.run_id, stage.name),
        )


def mark_stage_completed(ctx: BackfillContext, stage: Stage, payload: dict[str, Any]) -> None:
    now = utc_now_iso()
    output_count = int(payload.get("output_count") or payload.get("row_count") or 0)
    warning_count = int(payload.get("warning_count") or 0)
    error_count = int(payload.get("error_count") or 0)
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        conn.execute(
            """UPDATE research_backfill_stage_runs
                  SET status='completed',
                      output_count=?,
                      warning_count=?,
                      error_count=?,
                      completed_at=?,
                      payload_json=?
                WHERE run_id=? AND stage_name=?""",
            (
                output_count,
                warning_count,
                error_count,
                now,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ctx.run_id,
                stage.name,
            ),
        )


def mark_stage_failed(ctx: BackfillContext, stage: Stage, exc: BaseException) -> None:
    now = utc_now_iso()
    error = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        conn.execute(
            """UPDATE research_backfill_stage_runs
                  SET status='failed',
                      error_count=error_count+1,
                      completed_at=?,
                      error_message=?
                WHERE run_id=? AND stage_name=?""",
            (now, error, ctx.run_id, stage.name),
        )
        conn.execute(
            "UPDATE research_backfill_runs SET status='failed', error_message=? WHERE run_id=?",
            (error, ctx.run_id),
        )


def record_artifact(
    ctx: BackfillContext,
    artifact_type: str,
    path: Path,
    *,
    row_count: int = 0,
) -> None:
    if not path.exists():
        return
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        conn.execute(
            """INSERT OR REPLACE INTO research_build_artifacts(
                   run_id,artifact_type,path,row_count,checksum,created_at
               ) VALUES (?,?,?,?,?,?)""",
            (ctx.run_id, artifact_type, str(path), int(row_count), digest, utc_now_iso()),
        )


def write_artifact(
    ctx: BackfillContext,
    artifact_type: str,
    name: str,
    payload: dict[str, Any],
    *,
    row_count: int = 0,
) -> Path:
    ctx.output_dir.mkdir(parents=True, exist_ok=True)
    path = ctx.output_dir / f"{ctx.run_id}_{_safe_name(name)}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    record_artifact(ctx, artifact_type, path, row_count=row_count)
    return path


def _subprocess_env(ctx: BackfillContext) -> dict[str, str]:
    env = dict(os.environ)
    env["DATABASE_PATH"] = str(ctx.database_path)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _local_date_window(ctx: BackfillContext) -> tuple[str, str]:
    tz = ZoneInfo(ctx.timezone_name)
    return ctx.start_utc.astimezone(tz).date().isoformat(), ctx.end_utc.astimezone(tz).date().isoformat()


def _resume_marketaux_start_date(ctx: BackfillContext, start_date: str, end_date: str) -> tuple[str | None, dict[str, Any]]:
    """Return the remaining Marketaux start date for resume runs.

    The canonical build window remains anchored by config, but a failed/resumed
    S03 source stage does not need to re-query already persisted Marketaux
    windows. Full/forced rebuilds still use the configured start date.
    """

    payload: dict[str, Any] = {
        "configured_start_date": start_date,
        "configured_end_date": end_date,
        "resume_enabled": bool(ctx.resume and not ctx.force_stage),
        "latest_existing_marketaux_date": None,
        "effective_start_date": start_date,
        "skip_reason": "",
    }
    if not ctx.resume or ctx.force_stage:
        return start_date, payload

    with connect(ctx.database_path) as conn:
        latest = conn.execute(
            """
            SELECT date(MAX(published_at_utc))
            FROM raw_news_articles
            WHERE source_system = 'marketaux'
            """
        ).fetchone()[0]

    if not latest:
        return start_date, payload

    payload["latest_existing_marketaux_date"] = latest
    next_date = (datetime.strptime(str(latest), "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
    if next_date > end_date:
        payload["effective_start_date"] = None
        payload["skip_reason"] = "marketaux_raw_news_already_covers_configured_end_date"
        return None, payload
    if next_date > start_date:
        payload["effective_start_date"] = next_date
        payload["skip_reason"] = "resume_from_next_uncovered_marketaux_date"
        return next_date, payload
    return start_date, payload


def _deterministic_client_id(ctx: BackfillContext, offset: int) -> int:
    digest = hashlib.sha256(f"{ctx.run_id}:{offset}".encode("utf-8")).hexdigest()
    return 25000 + (int(digest[:8], 16) % 30000)


def _configured_symbols(ctx: BackfillContext) -> str:
    configured = _config_value(ctx.config, "sources", "news", "symbols")
    if isinstance(configured, str) and configured.strip():
        return ",".join(item.strip().upper() for item in configured.split(",") if item.strip())
    symbols: list[str] = []
    universes = ctx.config.get("universes") or {}
    for section_name in ("news_subscription_universe", "outcome_core_universe", "event_proxy_universe"):
        section = universes.get(section_name) if isinstance(universes, dict) else None
        if not isinstance(section, dict):
            continue
        for value in section.values():
            if isinstance(value, str):
                symbols.extend(item.strip().upper() for item in value.split(",") if item.strip())
            elif isinstance(value, list):
                symbols.extend(str(item).strip().upper() for item in value if str(item).strip())
    if not symbols:
        symbols = [item.strip().upper() for item in DEFAULT_CANONICAL_SYMBOLS.split(",") if item.strip()]
    return ",".join(dict.fromkeys(symbols))


def _command_timeout(ctx: BackfillContext, source_key: str, default: int) -> int:
    return _int_config(ctx.config, "runtime", "timeouts_seconds", source_key, default=default)


def _configured_ibkr_news_providers(ibkr_cfg: Any) -> str:
    preferred: Any = None
    if isinstance(ibkr_cfg, dict):
        preferred = ibkr_cfg.get("providers_preferred")
    if isinstance(preferred, str):
        codes = [item.strip().upper() for item in preferred.split(",") if item.strip()]
    elif isinstance(preferred, list):
        codes = [str(item).strip().upper() for item in preferred if str(item).strip()]
    else:
        codes = list(DEFAULT_IBKR_NEWS_PROVIDER_CODES)
    if not codes:
        codes = list(DEFAULT_IBKR_NEWS_PROVIDER_CODES)
    return ",".join(dict.fromkeys(codes))


def _json_from_command_stdout(payload: dict[str, Any]) -> dict[str, Any] | None:
    stdout = str(payload.get("stdout_tail") or "").strip()
    if not stdout:
        return None
    start = stdout.find("{")
    end = stdout.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        parsed = json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _assert_ibkr_news_backfill_complete(payload: dict[str, Any]) -> None:
    result = _json_from_command_stdout(payload)
    if not result:
        raise RuntimeError("S03/ibkr_news_backfill did not emit a parseable JSON summary")
    if result.get("timed_out"):
        raise RuntimeError(
            "S03/ibkr_news_backfill timed out before completing historical news pagination; "
            f"summary={json.dumps(result, ensure_ascii=False, sort_keys=True)}"
        )


def run_main_command(
    ctx: BackfillContext,
    stage_name: str,
    label: str,
    args: list[str],
    *,
    timeout_seconds: int = 1800,
    allow_failure: bool = False,
) -> dict[str, Any]:
    started_at = utc_now_iso()
    timeout_limit = float(timeout_seconds or 0)
    subprocess_timeout = timeout_limit if timeout_limit > 0 else None
    completed = subprocess.run(
        [sys.executable, "main.py", *args],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
        timeout=subprocess_timeout,
        env=_subprocess_env(ctx),
    )
    payload = {
        "stage": stage_name,
        "label": label,
        "args": args,
        "returncode": completed.returncode,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "timeout_seconds": timeout_seconds,
        "timeout_enabled": subprocess_timeout is not None,
        "stdout_tail": completed.stdout[-8000:],
        "stderr_tail": completed.stderr[-8000:],
        "allow_failure": allow_failure,
        "output_count": 1,
        "error_count": 1 if completed.returncode and not allow_failure else 0,
    }
    write_artifact(ctx, f"{stage_name}_{label}", f"{stage_name}_{label}", payload, row_count=1)
    if completed.returncode and not allow_failure:
        raise RuntimeError(
            f"{stage_name}/{label} failed with returncode={completed.returncode}: "
            f"{completed.stderr[-1000:] or completed.stdout[-1000:]}"
        )
    return payload


def _split_symbol_csv(symbols: str) -> list[str]:
    return list(dict.fromkeys(item.strip().upper() for item in str(symbols or "").split(",") if item.strip()))


def _parse_iso_date(value: str) -> datetime:
    return datetime.fromisoformat(value[:10])


def _conversion_provider_where(source: str) -> tuple[str, str, str, str]:
    normalized = source.strip().lower()
    if normalized == "benzinga":
        return (
            "provider='BZ'",
            "benzinga",
            "bz:",
            "benzinga-external-signals-build",
        )
    if normalized == "dow_jones":
        return (
            "upper(provider) LIKE 'DJ%'",
            "dow_jones",
            "dj:",
            "dow-jones-external-signals-build",
        )
    raise ValueError(f"Unsupported external signal conversion source: {source}")


def _build_conversion_work_items(
    ctx: BackfillContext,
    *,
    source: str,
    label: str,
    start_date: str,
    end_date: str,
    symbols: str,
    skip_completed: bool,
    max_slice_days: int,
    target_rows_per_shard: int,
) -> list[ExternalSignalConversionWorkItem]:
    provider_where, signal_source, external_id_prefix, command_name = _conversion_provider_where(source)
    cleaned_symbols = _split_symbol_csv(symbols)
    if not cleaned_symbols:
        return []
    max_slice_days = max(1, int(max_slice_days or 1))
    target_rows_per_shard = max(1, int(target_rows_per_shard or 1))
    time_expr = "COALESCE(NULLIF(provider_published_at,''), published_at, received_at, created_at)"
    missing_clause = ""
    params: list[Any] = [start_date, end_date, *cleaned_symbols]
    if skip_completed:
        missing_clause = """
           AND NOT EXISTS (
                   SELECT 1
                     FROM external_market_signals e
                    WHERE e.source=?
                      AND e.external_id=(? || CAST(n.id AS TEXT))
                      AND e.source_news_id IS NOT NULL
               )
        """
        params.extend([signal_source, external_id_prefix])

    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        rows = conn.execute(
            f"""
            SELECT upper(primary_symbol) AS symbol,
                   substr({time_expr},1,10) AS published_date,
                   COUNT(*) AS row_count
              FROM news_articles n
             WHERE {provider_where}
               AND substr({time_expr},1,10)>=?
               AND substr({time_expr},1,10)<=?
               AND upper(primary_symbol) IN ({','.join('?' for _ in cleaned_symbols)})
               {missing_clause}
             GROUP BY upper(primary_symbol), substr({time_expr},1,10)
             ORDER BY upper(primary_symbol), substr({time_expr},1,10)
            """,
            tuple(params),
        ).fetchall()

    by_symbol: dict[str, list[tuple[str, int]]] = {}
    for row in rows:
        symbol = str(row["symbol"] or "").upper()
        day = str(row["published_date"] or "")[:10]
        if not symbol or not day:
            continue
        by_symbol.setdefault(symbol, []).append((day, int(row["row_count"] or 0)))

    items: list[ExternalSignalConversionWorkItem] = []
    for symbol, daily_counts in by_symbol.items():
        current_start = ""
        current_end = ""
        current_rows = 0
        for day, row_count in daily_counts:
            if not current_start:
                current_start = day
                current_end = day
                current_rows = row_count
                continue
            span_days = (_parse_iso_date(day) - _parse_iso_date(current_start)).days + 1
            should_flush = (current_rows >= target_rows_per_shard) or (span_days > max_slice_days)
            if should_flush:
                items.append(
                    ExternalSignalConversionWorkItem(
                        command_name=command_name,
                        label=label,
                        source=signal_source,
                        external_id_prefix=external_id_prefix,
                        symbol=symbol,
                        start_date=current_start,
                        end_date=current_end,
                        estimated_rows=current_rows,
                    )
                )
                current_start = day
                current_end = day
                current_rows = row_count
            else:
                current_end = day
                current_rows += row_count
        if current_start:
            items.append(
                ExternalSignalConversionWorkItem(
                    command_name=command_name,
                    label=label,
                    source=signal_source,
                    external_id_prefix=external_id_prefix,
                    symbol=symbol,
                    start_date=current_start,
                    end_date=current_end,
                    estimated_rows=current_rows,
                )
            )

    return sorted(items, key=lambda item: (item.estimated_rows, item.symbol), reverse=True)


def _conversion_work_item_args(
    item: ExternalSignalConversionWorkItem,
    *,
    conversion_limit: int,
    include_non_geopolitical: bool,
) -> list[str]:
    args = [
        item.command_name,
        "--start",
        item.start_date,
        "--end",
        item.end_date,
        "--symbols",
        item.symbol,
        "--limit",
        str(int(conversion_limit)),
    ]
    if include_non_geopolitical:
        args.append("--include-non-geopolitical")
    return args


def _conversion_repair_snapshot(
    ctx: BackfillContext,
    *,
    source: str,
    label: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    provider_where, signal_source, external_id_prefix, _command_name = _conversion_provider_where(source)
    time_expr = "COALESCE(NULLIF(provider_published_at,''), published_at, received_at, created_at)"
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        raw_total = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM news_articles n
                 WHERE {provider_where}
                   AND substr({time_expr},1,10)>=?
                   AND substr({time_expr},1,10)<=?
                """,
                (start_date, end_date),
            ).fetchone()[0]
            or 0
        )
        converted_total = int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM news_articles n
                 WHERE {provider_where}
                   AND substr({time_expr},1,10)>=?
                   AND substr({time_expr},1,10)<=?
                   AND EXISTS (
                         SELECT 1
                           FROM external_market_signals e
                          WHERE e.source=?
                            AND e.external_id=(? || CAST(n.id AS TEXT))
                            AND e.source_news_id IS NOT NULL
                       )
                """,
                (start_date, end_date, signal_source, external_id_prefix),
            ).fetchone()[0]
            or 0
        )
        missing_lineage_signals = int(
            conn.execute(
                """
                SELECT COUNT(*)
                  FROM external_market_signals
                 WHERE source=?
                   AND COALESCE(source_news_id,0)=0
                """,
                (signal_source,),
            ).fetchone()[0]
            or 0
        )
        duplicate_external_ids = int(
            conn.execute(
                """
                SELECT COUNT(*)
                  FROM (
                        SELECT external_id
                          FROM external_market_signals
                         WHERE source=?
                           AND COALESCE(external_id,'')!=''
                         GROUP BY external_id
                        HAVING COUNT(*) > 1
                  )
                """,
                (signal_source,),
            ).fetchone()[0]
            or 0
        )
    remaining_total = max(0, raw_total - converted_total)
    payload = {
        "stage": "S03_m251a_news_raw_backfill",
        "label": label,
        "source": source,
        "repair_mode": "row_level_resume_after_interrupted_conversion",
        "raw_total": raw_total,
        "converted_total": converted_total,
        "remaining_total": remaining_total,
        "missing_lineage_signals": missing_lineage_signals,
        "duplicate_external_ids": duplicate_external_ids,
        "idempotency_key": f"{signal_source}:{external_id_prefix}<news_id>",
        "created_at": utc_now_iso(),
    }
    write_artifact(
        ctx,
        f"S03_m251a_news_raw_backfill_{label}_repair_scan",
        f"S03_m251a_news_raw_backfill_{label}_repair_scan",
        payload,
        row_count=remaining_total,
    )
    if missing_lineage_signals or duplicate_external_ids:
        raise RuntimeError(
            f"S03/{label} repair scan failed: "
            f"missing_lineage_signals={missing_lineage_signals}, "
            f"duplicate_external_ids={duplicate_external_ids}"
        )
    return payload


def _ibkr_news_raw_row_count(ctx: BackfillContext, *, start_date: str, end_date: str) -> int:
    time_expr = "COALESCE(NULLIF(provider_published_at,''), published_at, received_at, created_at)"
    with connect(ctx.database_path) as conn:
        if not _table_exists(conn, "news_articles"):
            return 0
        return int(
            conn.execute(
                f"""
                SELECT COUNT(*)
                  FROM news_articles
                 WHERE (provider='BZ' OR upper(provider) LIKE 'DJ%')
                   AND substr({time_expr},1,10)>=?
                   AND substr({time_expr},1,10)<=?
                """,
                (start_date, end_date),
            ).fetchone()[0]
            or 0
        )


def _iter_iso_dates(start_date: str, end_date: str) -> list[str]:
    start = datetime.strptime(start_date[:10], "%Y-%m-%d").date()
    end = datetime.strptime(end_date[:10], "%Y-%m-%d").date()
    if end < start:
        return []
    days: list[str] = []
    current = start
    while current <= end:
        days.append(current.isoformat())
        current += timedelta(days=1)
    return days


def _slice_tracking_stage_name(stage_name: str, label: str) -> str:
    return f"{stage_name}:{label}"


def _completed_slice_dates(ctx: BackfillContext, slice_stage_name: str) -> set[str]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        return {
            str(row["slice_start_utc"])[:10]
            for row in conn.execute(
                """SELECT slice_start_utc
                     FROM research_backfill_stage_slices
                    WHERE run_id=?
                      AND stage_name=?
                      AND status='completed'
                      AND slice_end_utc NOT LIKE '%#%'""",
                (ctx.run_id, slice_stage_name),
            ).fetchall()
            if str(row["slice_start_utc"] or "")
        }


def _completed_slice_end_keys(ctx: BackfillContext, slice_stage_name: str) -> set[str]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        return {
            str(row["slice_end_utc"] or "")
            for row in conn.execute(
                """SELECT slice_end_utc
                     FROM research_backfill_stage_slices
                    WHERE run_id=?
                      AND stage_name=?
                      AND status='completed'""",
                (ctx.run_id, slice_stage_name),
            ).fetchall()
            if str(row["slice_end_utc"] or "")
        }


def _failed_slice_end_keys(ctx: BackfillContext, slice_stage_name: str) -> set[str]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        return {
            str(row["slice_end_utc"] or "")
            for row in conn.execute(
                """SELECT slice_end_utc
                     FROM research_backfill_stage_slices
                    WHERE run_id=?
                      AND stage_name=?
                      AND status='failed'""",
                (ctx.run_id, slice_stage_name),
            ).fetchall()
            if str(row["slice_end_utc"] or "")
        }


def _marketaux_raw_dates(ctx: BackfillContext) -> set[str]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        return {
            str(row["published_date"])[:10]
            for row in conn.execute(
                """
                SELECT DISTINCT substr(COALESCE(NULLIF(published_at_utc,''),received_at_utc,created_at),1,10) AS published_date
                  FROM raw_news_articles
                 WHERE source_system='marketaux'
                    OR provider='marketaux'
                """
            ).fetchall()
            if str(row["published_date"] or "")
        }


def _marketaux_signal_linked_dates(ctx: BackfillContext) -> set[str]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        if not _table_exists(conn, "raw_news_articles") or not _table_exists(conn, "external_market_signals"):
            return set()
        return {
            str(row["published_date"])[:10]
            for row in conn.execute(
                """
                SELECT DISTINCT substr(COALESCE(NULLIF(n.published_at_utc,''),n.received_at_utc,n.created_at),1,10) AS published_date
                  FROM raw_news_articles n
                  JOIN external_market_signals e
                    ON e.source='marketaux'
                   AND e.source_news_id=n.id
                 WHERE n.source_system='marketaux'
                    OR n.provider='marketaux'
                """
            ).fetchall()
            if str(row["published_date"] or "")
        }


def _mark_stage_slice(
    ctx: BackfillContext,
    slice_stage_name: str,
    item: ExternalSignalConversionWorkItem,
    *,
    status: str,
    output_count: int = 0,
    last_error: str = "",
    started_at: str = "",
    completed_at: str = "",
) -> None:
    now = utc_now_iso()
    slice_end = item.slice_key or item.end_date
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        conn.execute(
            """INSERT OR IGNORE INTO research_backfill_stage_slices(
                   run_id,stage_name,slice_start_utc,slice_end_utc,status,attempt_count,
                   output_count,last_error,started_at,completed_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                ctx.run_id,
                slice_stage_name,
                item.start_date,
                slice_end,
                "pending",
                0,
                0,
                "",
                "",
                "",
            ),
        )
        if status == "running":
            conn.execute(
                """UPDATE research_backfill_stage_slices
                      SET status='running',
                          attempt_count=attempt_count+1,
                          last_error='',
                          started_at=?,
                          completed_at=''
                    WHERE run_id=? AND stage_name=? AND slice_start_utc=? AND slice_end_utc=?""",
                (started_at or now, ctx.run_id, slice_stage_name, item.start_date, slice_end),
            )
        else:
            conn.execute(
                """UPDATE research_backfill_stage_slices
                      SET status=?,
                          output_count=?,
                          last_error=?,
                          completed_at=?
                    WHERE run_id=? AND stage_name=? AND slice_start_utc=? AND slice_end_utc=?""",
                (
                    status,
                    int(output_count),
                    str(last_error or "")[-4000:],
                    completed_at or now,
                    ctx.run_id,
                    slice_stage_name,
                    item.start_date,
                    slice_end,
                ),
            )


def _marketaux_route_work_specs(marketaux_mode: str = "hybrid") -> list[tuple[str, str, str | None]]:
    from recorder.external_market_sources import MARKETAUX_BACKFILL_QUERIES

    normalized_mode = str(marketaux_mode or "hybrid").strip().lower()
    specs: list[tuple[str, str, str | None]] = []
    if normalized_mode in {"broad", "hybrid"}:
        specs.append(("MARKETAUX_BROAD", "broad", None))
    if normalized_mode in {"query", "hybrid"}:
        specs.extend(
            (f"MARKETAUX_QUERY_{query_name}", "query", query_name)
            for query_name, _ in MARKETAUX_BACKFILL_QUERIES
        )
    return specs


def _build_marketaux_work_items(
    ctx: BackfillContext,
    *,
    label: str,
    start_date: str,
    end_date: str,
    skip_completed: bool,
    marketaux_mode: str = "hybrid",
) -> list[ExternalSignalConversionWorkItem]:
    all_days = _iter_iso_dates(start_date, end_date)
    slice_stage_name = _slice_tracking_stage_name("S03_m251a_news_raw_backfill", label)
    completed = _completed_slice_dates(ctx, slice_stage_name)
    completed_end_keys = _completed_slice_end_keys(ctx, slice_stage_name)
    failed_end_keys = _failed_slice_end_keys(ctx, slice_stage_name)
    signal_linked_dates = _marketaux_signal_linked_dates(ctx)
    items: list[ExternalSignalConversionWorkItem] = []
    route_specs = _marketaux_route_work_specs(marketaux_mode)
    for day in all_days:
        if skip_completed and day in completed:
            continue
        for route_symbol, _mode, _query_name in route_specs:
            slice_key = f"{day}#{route_symbol}"
            if skip_completed and slice_key in completed_end_keys:
                continue
            if (
                skip_completed
                and route_symbol == "MARKETAUX_BROAD"
                and day in signal_linked_dates
                and slice_key not in failed_end_keys
            ):
                continue
            items.append(
                ExternalSignalConversionWorkItem(
                    command_name="external-market-backfill",
                    label=label,
                    source="marketaux",
                    external_id_prefix="",
                    symbol=route_symbol,
                    start_date=day,
                    end_date=day,
                    estimated_rows=1,
                    slice_key=slice_key,
                )
            )
    return items


def _marketaux_route_mode_and_query(item: ExternalSignalConversionWorkItem, default_mode: str) -> tuple[str, str | None]:
    if item.symbol == "MARKETAUX_BROAD":
        return "broad", None
    if item.symbol.startswith("MARKETAUX_QUERY_"):
        return "query", item.symbol.removeprefix("MARKETAUX_QUERY_")
    return str(default_mode or "hybrid"), None


def _marketaux_work_item_args(
    item: ExternalSignalConversionWorkItem,
    *,
    symbols: str,
    marketaux_cfg: dict[str, Any],
    marketaux_params: dict[str, Any],
    ctx: BackfillContext,
) -> list[str]:
    mode, query_name = _marketaux_route_mode_and_query(
        item,
        str(marketaux_cfg.get("mode", "hybrid") if isinstance(marketaux_cfg, dict) else "hybrid"),
    )
    args = [
        "external-market-backfill",
        "--source",
        "marketaux",
        "--start",
        item.start_date,
        "--end",
        item.end_date,
        "--window-days",
        "1",
        "--limit-per-window",
        str(_int_config(ctx.config, "sources", "news", "marketaux", "limit_per_window", default=100)),
        "--pause-seconds",
        str(marketaux_cfg.get("pause_seconds", 0.75) if isinstance(marketaux_cfg, dict) else 0.75),
        "--marketaux-mode",
        mode,
        "--marketaux-entity-types",
        str(marketaux_params.get("entity_types", "equity,index,etf") if isinstance(marketaux_params, dict) else "equity,index,etf"),
        "--marketaux-countries",
        str(marketaux_params.get("countries", "us") if isinstance(marketaux_params, dict) else "us"),
        "--marketaux-symbols",
        symbols,
    ]
    if query_name:
        args.extend(["--marketaux-query-name", query_name])
    if isinstance(marketaux_params, dict) and not bool(marketaux_params.get("group_similar", True)):
        args.append("--marketaux-no-group-similar")
    if isinstance(marketaux_params, dict) and not bool(marketaux_params.get("must_have_entities", True)):
        args.append("--marketaux-allow-no-entities")
    return args


def run_main_commands_dynamic_queue(
    ctx: BackfillContext,
    stage_name: str,
    label: str,
    jobs: list[tuple[ExternalSignalConversionWorkItem, list[str]]],
    *,
    max_workers: int,
    timeout_seconds: int,
    allow_failure: bool = False,
    slice_tracking_stage_name: str | None = None,
) -> dict[str, Any]:
    if not jobs:
        payload = {
            "stage": stage_name,
            "label": label,
            "parallel_batch": True,
            "worker_mode": "dynamic_queue",
            "max_workers": max(1, int(max_workers)),
            "queued_count": 0,
            "completed_count": 0,
            "failed_count": 0,
            "skipped": True,
            "reason": "no_conversion_work_items",
            "returncode": 0,
            "output_count": 0,
            "error_count": 0,
        }
        write_artifact(ctx, f"{stage_name}_{label}_parallel_batch", f"{stage_name}_{label}_parallel_batch", payload)
        return payload

    worker_count = max(1, min(int(max_workers or 1), len(jobs)))
    queued = deque(enumerate(jobs, start=1))
    active: dict[int, dict[str, Any]] = {}
    completed_payloads: list[dict[str, Any]] = []
    failed_payloads: list[dict[str, Any]] = []
    started_at = utc_now_iso()
    timeout_limit = float(timeout_seconds or 0)
    timeout_enabled = timeout_limit > 0

    def launch_next() -> None:
        if not queued:
            return
        shard_index, (item, args) = queued.popleft()
        process = subprocess.Popen(
            [sys.executable, "main.py", *args],
            cwd=Path.cwd(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_subprocess_env(ctx),
        )
        active[shard_index] = {
            "process": process,
            "item": item,
            "args": args,
            "started_at": utc_now_iso(),
            "started_monotonic": time.monotonic(),
        }
        if slice_tracking_stage_name:
            _mark_stage_slice(
                ctx,
                slice_tracking_stage_name,
                item,
                status="running",
                started_at=active[shard_index]["started_at"],
            )

    for _ in range(worker_count):
        launch_next()

    while active:
        for shard_index, record in list(active.items()):
            process: subprocess.Popen[str] = record["process"]
            timed_out = timeout_enabled and (
                (time.monotonic() - float(record["started_monotonic"])) > timeout_limit
            )
            returncode = process.poll()
            if returncode is None and not timed_out:
                continue
            if timed_out and returncode is None:
                process.kill()
            stdout, stderr = process.communicate()
            returncode = process.returncode
            item: ExternalSignalConversionWorkItem = record["item"]
            shard_payload = {
                "stage": stage_name,
                "label": label,
                "shard_index": shard_index,
                "parallel_batch": True,
                "worker_mode": "dynamic_queue",
                "command_name": item.command_name,
                "source": item.source,
                "symbol": item.symbol,
                "start": item.start_date,
                "end": item.end_date,
                "estimated_rows": item.estimated_rows,
                "args": record["args"],
                "returncode": returncode,
                "timed_out": bool(timed_out),
                "timeout_seconds": timeout_seconds,
                "timeout_enabled": timeout_enabled,
                "started_at": record["started_at"],
                "completed_at": utc_now_iso(),
                "stdout_tail": stdout[-8000:],
                "stderr_tail": stderr[-8000:],
                "allow_failure": allow_failure,
                "output_count": 1,
                "error_count": 1 if (returncode or timed_out) and not allow_failure else 0,
            }
            write_artifact(
                ctx,
                f"{stage_name}_{label}_shard",
                f"{stage_name}_{label}_shard_{shard_index:04d}_{item.source}_{item.symbol}_{item.start_date}_{item.end_date}",
                shard_payload,
                row_count=max(1, int(item.estimated_rows)),
            )
            if slice_tracking_stage_name:
                _mark_stage_slice(
                    ctx,
                    slice_tracking_stage_name,
                    item,
                    status="completed" if returncode == 0 and not timed_out else "failed",
                    output_count=max(1, int(item.estimated_rows)) if returncode == 0 and not timed_out else 0,
                    last_error="" if returncode == 0 and not timed_out else (stderr[-4000:] or stdout[-4000:] or "timed_out"),
                    completed_at=shard_payload["completed_at"],
                )
            active.pop(shard_index, None)
            if (returncode or timed_out) and not allow_failure:
                failed_payloads.append(shard_payload)
                for active_record in active.values():
                    active_process: subprocess.Popen[str] = active_record["process"]
                    if active_process.poll() is None:
                        active_process.kill()
                for active_shard_index, active_record in list(active.items()):
                    active_stdout, active_stderr = active_record["process"].communicate()
                    active_item: ExternalSignalConversionWorkItem = active_record["item"]
                    cancelled_payload = {
                        "stage": stage_name,
                        "label": label,
                        "shard_index": active_shard_index,
                        "parallel_batch": True,
                        "worker_mode": "dynamic_queue",
                        "command_name": active_item.command_name,
                        "source": active_item.source,
                        "symbol": active_item.symbol,
                        "start": active_item.start_date,
                        "end": active_item.end_date,
                        "estimated_rows": active_item.estimated_rows,
                        "args": active_record["args"],
                        "returncode": active_record["process"].returncode,
                        "cancelled_due_to_peer_failure": True,
                        "timeout_seconds": timeout_seconds,
                        "timeout_enabled": timeout_enabled,
                        "started_at": active_record["started_at"],
                        "completed_at": utc_now_iso(),
                        "stdout_tail": active_stdout[-8000:],
                        "stderr_tail": active_stderr[-8000:],
                        "allow_failure": allow_failure,
                        "output_count": 1,
                        "error_count": 1,
                    }
                    write_artifact(
                        ctx,
                        f"{stage_name}_{label}_shard",
                        f"{stage_name}_{label}_shard_{active_shard_index:04d}_{active_item.source}_{active_item.symbol}_{active_item.start_date}_{active_item.end_date}",
                        cancelled_payload,
                        row_count=max(1, int(active_item.estimated_rows)),
                    )
                    if slice_tracking_stage_name:
                        _mark_stage_slice(
                            ctx,
                            slice_tracking_stage_name,
                            active_item,
                            status="failed",
                            output_count=0,
                            last_error=active_stderr[-4000:] or active_stdout[-4000:] or "cancelled_due_to_peer_failure",
                            completed_at=cancelled_payload["completed_at"],
                        )
                    failed_payloads.append(cancelled_payload)
                active.clear()
                break
            completed_payloads.append(shard_payload)
            launch_next()
        if failed_payloads and not allow_failure:
            break
        if active:
            time.sleep(0.25)

    total_estimated_rows = sum(int(item.estimated_rows) for item, _args in jobs)
    payload = {
        "stage": stage_name,
        "label": label,
        "parallel_batch": True,
        "worker_mode": "dynamic_queue",
        "max_workers": worker_count,
        "queued_count": len(jobs),
        "completed_count": len(completed_payloads),
        "failed_count": len(failed_payloads),
        "estimated_rows": total_estimated_rows,
        "started_at": started_at,
        "completed_at": utc_now_iso(),
        "timeout_seconds": timeout_seconds,
        "timeout_enabled": timeout_enabled,
        "shards": [
            {
                "shard_index": row["shard_index"],
                "source": row["source"],
                "symbol": row["symbol"],
                "start": row["start"],
                "end": row["end"],
                "estimated_rows": row["estimated_rows"],
                "returncode": row["returncode"],
                "timed_out": row.get("timed_out", False),
            }
            for row in completed_payloads + failed_payloads
        ],
        "returncode": 1 if failed_payloads and not allow_failure else 0,
        "output_count": max(1, total_estimated_rows),
        "error_count": len(failed_payloads) if not allow_failure else 0,
    }
    write_artifact(
        ctx,
        f"{stage_name}_{label}_parallel_batch",
        f"{stage_name}_{label}_parallel_batch",
        payload,
        row_count=max(1, total_estimated_rows),
    )
    if failed_payloads and not allow_failure:
        first = failed_payloads[0]
        raise RuntimeError(
            f"{stage_name}/{label} shard failed "
            f"source={first.get('source')} symbol={first.get('symbol')} "
            f"date={first.get('start')}..{first.get('end')} returncode={first.get('returncode')}: "
            f"{first.get('stderr_tail') or first.get('stdout_tail')}"
        )
    return payload


def _source_summary_counts(ctx: BackfillContext) -> dict[str, int]:
    with connect(ctx.database_path) as conn:
        return {
            table: _count(conn, table)
            for table in (
                "news_articles",
                "raw_news_articles",
                "external_market_signals",
                "provider_entities",
                "news_asset_candidates",
                "news_event_clusters",
                "news_event_cluster_roles",
                "fred_observations",
                "economic_indicator_observations",
                "macro_event_releases",
                "macro_observations",
                "macro_event_cluster_links",
            )
        }


def _macro_source_input_counts(ctx: BackfillContext, start_date: str, end_date: str) -> dict[str, int]:
    with connect(ctx.database_path) as conn:
        counts: dict[str, int] = {"fred": 0, "economic_indicators": 0}
        if _table_exists(conn, "fred_observations"):
            counts["fred"] = int(
                conn.execute(
                    """SELECT COUNT(*) AS c
                         FROM fred_observations
                        WHERE observation_date>=?
                          AND observation_date<=?""",
                    (start_date[:10], end_date[:10]),
                ).fetchone()["c"]
            )
        if _table_exists(conn, "economic_indicator_observations"):
            counts["economic_indicators"] = int(
                conn.execute(
                    """SELECT COUNT(*) AS c
                         FROM economic_indicator_observations
                        WHERE observation_date>=?
                          AND observation_date<=?""",
                    (start_date[:10], end_date[:10]),
                ).fetchone()["c"]
            )
        return counts


def _query_count(conn: sqlite3.Connection, sql: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM ({sql.rstrip().rstrip(';')})").fetchone()[0])


def _sample_rows(conn: sqlite3.Connection, sql: str, *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) LIMIT {int(limit)}").fetchall()
    ]


def _s03_idempotency_gate(ctx: BackfillContext) -> dict[str, Any]:
    checks = {
        "duplicate_mapping_run_key": """
            SELECT build_id,mapping_run_key,COUNT(*) AS cnt
              FROM news_asset_mapping_runs
             GROUP BY build_id,mapping_run_key
            HAVING COUNT(*) > 1
        """,
        "duplicate_candidate_key": """
            SELECT mapping_run_id,candidate_key,COUNT(*) AS cnt
              FROM news_asset_candidates
             GROUP BY mapping_run_id,candidate_key
            HAVING COUNT(*) > 1
        """,
        "marketaux_signal_missing_source_news_id": """
            SELECT id,source,external_id,title,source_news_id
              FROM external_market_signals
             WHERE source='marketaux'
               AND (source_news_id IS NULL OR source_news_id=0)
        """,
        "s03_signal_missing_source_news_id": """
            SELECT id,source,external_id,title,source_news_id
              FROM external_market_signals
             WHERE source IN ('marketaux','benzinga','dow_jones')
               AND (source_news_id IS NULL OR source_news_id=0)
        """,
        "candidate_missing_mapping_run_id": """
            SELECT id,news_id,event_cluster_id,symbol,role,source
              FROM news_asset_candidates
             WHERE mapping_run_id IS NULL
        """,
        "bad_spy_fallback_context": """
            SELECT id,news_id,event_cluster_id,symbol,role,source,tradable_candidate,evidence
              FROM news_asset_candidates
             WHERE upper(symbol)='SPY'
               AND (
                    lower(COALESCE(source,'')) LIKE '%fallback%'
                 OR lower(COALESCE(source,'')) LIKE '%context%'
                 OR lower(COALESCE(role,'')) LIKE '%fallback%'
                 OR lower(COALESCE(role,'')) LIKE '%context%'
                 OR lower(COALESCE(evidence,'')) LIKE '%fallback%'
                 OR lower(COALESCE(evidence,'')) LIKE '%context%'
               )
               AND (
                    tradable_candidate=1
                 OR lower(COALESCE(role,'')) IN ('primary','proxy','direct_entity')
               )
        """,
        "bad_route_only_tradable": """
            SELECT id,news_id,event_cluster_id,symbol,role,source,tradable_candidate,evidence
              FROM news_asset_candidates
             WHERE lower(COALESCE(role,''))='route_only'
               AND tradable_candidate=1
        """,
        "news_id_multiple_canonical_cluster_links": """
            SELECT news_id,COUNT(DISTINCT event_cluster_id) AS cluster_count
              FROM news_article_cluster_links
             GROUP BY news_id
            HAVING COUNT(DISTINCT event_cluster_id) > 1
        """,
    }
    gate: dict[str, Any] = {"checks": {}, "passed": True}
    with connect(ctx.database_path) as conn:
        for name, sql in checks.items():
            try:
                count = _query_count(conn, sql)
                samples = _sample_rows(conn, sql, limit=5) if count else []
            except sqlite3.Error as exc:
                count = 1
                samples = [{"error": str(exc)}]
            gate["checks"][name] = {"count": count, "samples": samples}
            if count:
                gate["passed"] = False
    gate["acceptance_criteria"] = [
        "Marketaux external signals must have source_news_id.",
        "S03 Marketaux/Benzinga/Dow Jones external signals must have source_news_id.",
        "Asset candidates must have mapping_run_id and candidate_key uniqueness.",
        "Mapping runs must have build_id/mapping_run_key uniqueness.",
        "SPY fallback_context and route_only rows must not be tradable candidates.",
        "Each raw news article should have at most one canonical event_cluster_id link.",
    ]
    return gate


def finalise_run_if_possible(ctx: BackfillContext, stages: list[Stage]) -> None:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        rows = conn.execute(
            "SELECT stage_name,status FROM research_backfill_stage_runs WHERE run_id=?",
            (ctx.run_id,),
        ).fetchall()
        status_by_stage = {str(row["stage_name"]): str(row["status"]) for row in rows}
        if all(status_by_stage.get(stage.name) == "completed" for stage in stages if not stage.blind_stage):
            conn.execute(
                """UPDATE research_backfill_runs
                      SET status='completed',
                          completed_at=CASE WHEN completed_at='' THEN ? ELSE completed_at END
                    WHERE run_id=? AND status!='failed'""",
                (utc_now_iso(), ctx.run_id),
            )


def _stage_summary_counts(db_path: Path) -> dict[str, int]:
    with connect(db_path) as conn:
        return {
            table: _count(conn, table)
            for table in (
                "news_articles",
                "raw_news_articles",
                "provider_entities",
                "news_asset_candidates",
                "news_event_clusters",
                "news_event_cluster_roles",
                "macro_event_releases",
                "macro_observations",
                "reaction_universe_plans",
                "reaction_universe_assets",
                "event_reaction_labels",
                "replay_features",
                "replay_outcomes",
            )
        }


def stage_preflight(ctx: BackfillContext) -> dict[str, Any]:
    problems: list[str] = []
    if ctx.start_utc >= ctx.end_utc:
        problems.append("start_utc must be before end_utc")
    if ctx.timezone_name != "America/New_York":
        problems.append("canonical research build should use America/New_York timezone")
    if not ctx.database_path.exists():
        problems.append(f"database does not exist yet: {ctx.database_path}")
    payload = {
        "stage": "S00_preflight",
        "runner_version": RESEARCH_BACKFILL_RUNNER_VERSION,
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_path": str(ctx.config_path),
        "config_hash": ctx.config_hash,
        "database_path": str(ctx.database_path),
        "timezone": ctx.timezone_name,
        "start_utc": ctx.start_iso,
        "end_utc": ctx.end_iso,
        "counts": _stage_summary_counts(ctx.database_path) if ctx.database_path.exists() else {},
        "problems": problems,
        "safety": {
            "broker_orders_submitted": False,
            "transmit_enabled": False,
            "live_order_routing_enabled": False,
            "live_thresholds_promoted": False,
        },
        "output_count": 1,
        "error_count": len(problems),
    }
    write_artifact(ctx, "preflight_report", "S00_preflight", payload, row_count=1)
    if problems:
        raise RuntimeError("; ".join(problems))
    return payload


def stage_schema_migration(ctx: BackfillContext) -> dict[str, Any]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
    payload = {
        "stage": "S01_schema_migration",
        "tables_ready": True,
        "counts": _stage_summary_counts(ctx.database_path),
        "output_count": 1,
    }
    write_artifact(ctx, "schema_migration_report", "S01_schema_migration", payload, row_count=1)
    return payload


def stage_build_manifest(ctx: BackfillContext) -> dict[str, Any]:
    payload = {
        "stage": "S02_build_manifest",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "versions": ctx.config.get("versions", {}),
        "start_utc": ctx.start_iso,
        "end_utc": ctx.end_iso,
        "counts": _stage_summary_counts(ctx.database_path),
        "output_count": 1,
    }
    write_artifact(ctx, "manifest", "S02_build_manifest", payload, row_count=1)
    return payload


def stage_m251a_news_raw_backfill(ctx: BackfillContext) -> dict[str, Any]:
    start_date, end_date = _local_date_window(ctx)
    symbols = _configured_symbols(ctx)
    commands: list[dict[str, Any]] = []
    before_counts = _source_summary_counts(ctx)
    news_cfg = ctx.config.get("sources", {}).get("news", {}) if isinstance(ctx.config.get("sources"), dict) else {}
    marketaux_cfg = news_cfg.get("marketaux", {}) if isinstance(news_cfg, dict) else {}
    ibkr_cfg = news_cfg.get("ibkr", {}) if isinstance(news_cfg, dict) else {}
    conversion_cfg = news_cfg.get("external_signal_conversion", {}) if isinstance(news_cfg, dict) else {}

    if _bool_config(ctx.config, "sources", "news", "marketaux", "enabled", default=True):
        marketaux_params = marketaux_cfg.get("params", {}) if isinstance(marketaux_cfg, dict) else {}
        marketaux_parallel_cfg = marketaux_cfg.get("parallel", {}) if isinstance(marketaux_cfg, dict) else {}
        marketaux_parallel_enabled = bool(
            marketaux_parallel_cfg.get("enabled", False) if isinstance(marketaux_parallel_cfg, dict) else False
        )
        marketaux_max_workers = max(
            1,
            int(marketaux_parallel_cfg.get("max_workers", 1) if isinstance(marketaux_parallel_cfg, dict) else 1),
        )
        marketaux_skip_completed = bool(
            marketaux_parallel_cfg.get("skip_completed_on_resume", True) if isinstance(marketaux_parallel_cfg, dict) else True
        )
        marketaux_resume_skip_completed = bool(ctx.resume and not ctx.force_stage and marketaux_skip_completed)
        marketaux_work_items = _build_marketaux_work_items(
            ctx,
            label="marketaux_backfill",
            start_date=start_date,
            end_date=end_date,
            skip_completed=marketaux_resume_skip_completed,
            marketaux_mode=str(marketaux_cfg.get("mode", "hybrid") if isinstance(marketaux_cfg, dict) else "hybrid"),
        )
        marketaux_work_summary = {
            "stage": "S03_m251a_news_raw_backfill",
            "label": "marketaux_backfill_work_scan",
            "source": "marketaux",
            "parallel_enabled": marketaux_parallel_enabled,
            "resume_skip_completed": marketaux_resume_skip_completed,
            "work_item_count": len(marketaux_work_items),
            "estimated_rows": sum(item.estimated_rows for item in marketaux_work_items),
            "max_workers": marketaux_max_workers if marketaux_parallel_enabled else 1,
            "returncode": 0,
            "output_count": len(marketaux_work_items),
            "error_count": 0,
        }
        commands.append(marketaux_work_summary)
        write_artifact(
            ctx,
            "S03_m251a_news_raw_backfill_marketaux_backfill_work_scan",
            "S03_m251a_news_raw_backfill_marketaux_backfill_work_scan",
            marketaux_work_summary,
            row_count=len(marketaux_work_items),
        )
        if not marketaux_work_items:
            commands.append(
                {
                    "stage": "S03_m251a_news_raw_backfill",
                    "label": "marketaux_backfill",
                    "skipped": True,
                    "reason": "marketaux_daily_shards_already_complete_on_resume"
                    if marketaux_resume_skip_completed
                    else "no_marketaux_date_shards",
                    "returncode": 0,
                    "output_count": 0,
                    "error_count": 0,
                }
            )
        elif marketaux_parallel_enabled or marketaux_resume_skip_completed:
            marketaux_jobs = [
                (
                    item,
                    _marketaux_work_item_args(
                        item,
                        symbols=symbols,
                        marketaux_cfg=marketaux_cfg if isinstance(marketaux_cfg, dict) else {},
                        marketaux_params=marketaux_params if isinstance(marketaux_params, dict) else {},
                        ctx=ctx,
                    ),
                )
                for item in marketaux_work_items
            ]
            commands.append(
                run_main_commands_dynamic_queue(
                    ctx,
                    "S03_m251a_news_raw_backfill",
                    "marketaux_backfill",
                    marketaux_jobs,
                    max_workers=marketaux_max_workers if marketaux_parallel_enabled else 1,
                    timeout_seconds=int(
                        marketaux_parallel_cfg.get(
                            "shard_timeout_seconds",
                            _command_timeout(ctx, "marketaux_backfill", 7200),
                        )
                        if isinstance(marketaux_parallel_cfg, dict)
                        else _command_timeout(ctx, "marketaux_backfill", 7200)
                    ),
                    slice_tracking_stage_name=_slice_tracking_stage_name(
                        "S03_m251a_news_raw_backfill",
                        "marketaux_backfill",
                    ),
                )
            )
        else:
            marketaux_args = _marketaux_work_item_args(
                ExternalSignalConversionWorkItem(
                    command_name="external-market-backfill",
                    label="marketaux_backfill",
                    source="marketaux",
                    external_id_prefix="",
                    symbol="MARKETAUX",
                    start_date=start_date,
                    end_date=end_date,
                    estimated_rows=1,
                ),
                symbols=symbols,
                marketaux_cfg=marketaux_cfg if isinstance(marketaux_cfg, dict) else {},
                marketaux_params=marketaux_params if isinstance(marketaux_params, dict) else {},
                ctx=ctx,
            )
            commands.append(
                run_main_command(
                    ctx,
                    "S03_m251a_news_raw_backfill",
                    "marketaux_backfill",
                    marketaux_args,
                    timeout_seconds=_command_timeout(ctx, "marketaux_backfill", 7200),
                )
            )

    if _bool_config(ctx.config, "sources", "news", "ibkr", "enabled", default=True):
        providers = _configured_ibkr_news_providers(ibkr_cfg)
        raw_skip_enabled = bool(
            ctx.resume
            and not ctx.force_stage
            and _bool_config(ctx.config, "sources", "news", "ibkr", "skip_on_resume_when_raw_exists", default=False)
        )
        raw_skip_min_rows = max(
            1,
            _int_config(ctx.config, "sources", "news", "ibkr", "skip_on_resume_min_raw_rows", default=1000),
        )
        raw_row_count = _ibkr_news_raw_row_count(ctx, start_date=start_date, end_date=end_date) if raw_skip_enabled else 0
        if raw_skip_enabled and raw_row_count >= raw_skip_min_rows:
            ibkr_result = {
                "stage": "S03_m251a_news_raw_backfill",
                "label": "ibkr_news_backfill",
                "skipped": True,
                "reason": "ibkr_raw_news_already_present_on_resume",
                "raw_row_count": raw_row_count,
                "raw_skip_min_rows": raw_skip_min_rows,
                "providers": providers,
                "returncode": 0,
                "output_count": raw_row_count,
                "error_count": 0,
            }
            write_artifact(
                ctx,
                "S03_m251a_news_raw_backfill_ibkr_news_backfill",
                "S03_m251a_news_raw_backfill_ibkr_news_backfill",
                ibkr_result,
                row_count=raw_row_count,
            )
            commands.append(ibkr_result)
        else:
            ibkr_args = [
                "ibkr-news-backfill",
                "--symbols",
                symbols,
                "--providers",
                providers,
                "--start",
                start_date,
                "--end",
                end_date,
                "--ibkr-client-id",
                str(_int_config(ctx.config, "sources", "news", "ibkr", "client_id", default=_deterministic_client_id(ctx, 11))),
                "--limit-per-symbol",
                str(_int_config(ctx.config, "sources", "news", "ibkr", "historical_max_headlines_per_request", default=300)),
                "--max-pages-per-symbol",
                str(_int_config(ctx.config, "sources", "news", "ibkr", "max_pages_per_symbol", default=80)),
                "--timeout",
                str(_int_config(ctx.config, "sources", "news", "ibkr", "timeout_seconds", default=21600)),
            ]
            ibkr_result = run_main_command(
                ctx,
                "S03_m251a_news_raw_backfill",
                "ibkr_news_backfill",
                ibkr_args,
                timeout_seconds=_command_timeout(ctx, "ibkr_news_backfill", 23400),
            )
            _assert_ibkr_news_backfill_complete(ibkr_result)
            commands.append(ibkr_result)

    if _bool_config(ctx.config, "sources", "news", "external_signal_conversion", "enabled", default=True):
        conversion_limit = _int_config(ctx.config, "sources", "news", "external_signal_conversion", "limit", default=100000)
        include_non_geopolitical = _bool_config(
            ctx.config,
            "sources",
            "news",
            "external_signal_conversion",
            "include_non_geopolitical",
            default=True,
        )
        parallel_cfg = conversion_cfg.get("parallel", {}) if isinstance(conversion_cfg, dict) else {}
        parallel_enabled = bool(parallel_cfg.get("enabled", False)) if isinstance(parallel_cfg, dict) else False
        max_workers = max(1, int(parallel_cfg.get("max_workers", 1) if isinstance(parallel_cfg, dict) else 1))
        max_slice_days = max(1, int(parallel_cfg.get("max_slice_days", 60) if isinstance(parallel_cfg, dict) else 60))
        target_rows_per_shard = max(
            1,
            int(parallel_cfg.get("target_rows_per_shard", 750) if isinstance(parallel_cfg, dict) else 750),
        )
        skip_completed_on_resume = bool(
            parallel_cfg.get("skip_completed_on_resume", True) if isinstance(parallel_cfg, dict) else True
        )
        resume_skip_completed = bool(ctx.resume and not ctx.force_stage and skip_completed_on_resume)
        for command_name, label in (
            ("benzinga-external-signals-build", "benzinga_external_signals_build"),
            ("dow-jones-external-signals-build", "dow_jones_external_signals_build"),
        ):
            source = "benzinga" if command_name.startswith("benzinga") else "dow_jones"
            pre_repair = _conversion_repair_snapshot(
                ctx,
                source=source,
                label=f"{label}_pre",
                start_date=start_date,
                end_date=end_date,
            )
            commands.append(pre_repair)
            work_items = _build_conversion_work_items(
                ctx,
                source=source,
                label=label,
                start_date=start_date,
                end_date=end_date,
                symbols=symbols,
                skip_completed=resume_skip_completed,
                max_slice_days=max_slice_days,
                target_rows_per_shard=target_rows_per_shard,
            )
            work_summary = {
                "stage": "S03_m251a_news_raw_backfill",
                "label": f"{label}_work_scan",
                "source": source,
                "parallel_enabled": parallel_enabled,
                "resume_skip_completed": resume_skip_completed,
                "work_item_count": len(work_items),
                "estimated_rows": sum(item.estimated_rows for item in work_items),
                "max_workers": max_workers if parallel_enabled else 1,
                "max_slice_days": max_slice_days,
                "target_rows_per_shard": target_rows_per_shard,
                "returncode": 0,
                "output_count": len(work_items),
                "error_count": 0,
            }
            commands.append(work_summary)
            write_artifact(
                ctx,
                f"S03_m251a_news_raw_backfill_{label}_work_scan",
                f"S03_m251a_news_raw_backfill_{label}_work_scan",
                work_summary,
                row_count=len(work_items),
            )
            if not work_items:
                commands.append(
                    {
                        "stage": "S03_m251a_news_raw_backfill",
                        "label": label,
                        "source": source,
                        "skipped": True,
                        "reason": "conversion_already_complete_on_resume" if resume_skip_completed else "no_source_rows",
                        "returncode": 0,
                        "output_count": 0,
                        "error_count": 0,
                    }
                )
                continue
            if parallel_enabled or resume_skip_completed:
                jobs = [
                    (
                        item,
                        _conversion_work_item_args(
                            item,
                            conversion_limit=conversion_limit,
                            include_non_geopolitical=include_non_geopolitical,
                        ),
                    )
                    for item in work_items
                ]
                commands.append(
                    run_main_commands_dynamic_queue(
                        ctx,
                        "S03_m251a_news_raw_backfill",
                        label,
                        jobs,
                        max_workers=max_workers if parallel_enabled else 1,
                        timeout_seconds=_command_timeout(ctx, label, 7200),
                    )
                )
            else:
                args = [
                    command_name,
                    "--start",
                    start_date,
                    "--end",
                    end_date,
                    "--symbols",
                    symbols,
                    "--limit",
                    str(conversion_limit),
                ]
                if include_non_geopolitical:
                    args.append("--include-non-geopolitical")
                commands.append(
                    run_main_command(
                        ctx,
                        "S03_m251a_news_raw_backfill",
                        label,
                        args,
                        timeout_seconds=_command_timeout(ctx, label, 7200),
                    )
                )
            post_repair = _conversion_repair_snapshot(
                ctx,
                source=source,
                label=f"{label}_post",
                start_date=start_date,
                end_date=end_date,
            )
            commands.append(post_repair)

    limit = ctx.config.get("run", {}).get("stage_limit")
    lineage_payload = rebuild_news_lineage_v2(
        str(ctx.database_path),
        start=ctx.start_iso,
        end=ctx.end_iso,
        limit=int(limit) if limit else None,
    )
    after_counts = _source_summary_counts(ctx)
    idempotency_gate = _s03_idempotency_gate(ctx)
    source_rows = int(after_counts.get("news_articles", 0)) + int(after_counts.get("raw_news_articles", 0)) + int(after_counts.get("external_market_signals", 0))
    payload = {
        "stage": "S03_m251a_news_raw_backfill",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "database_path": str(ctx.database_path),
        "start": start_date,
        "end": end_date,
        "symbols": symbols,
        "commands": commands,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "lineage": lineage_payload,
        "idempotency_gate": idempotency_gate,
        "output_count": int(lineage_payload.get("raw_news_scope_count") or after_counts.get("raw_news_articles") or source_rows),
        "source_rows": source_rows,
    }
    if _bool_config(ctx.config, "run", "require_non_empty_sources", default=True) and source_rows <= 0:
        payload["error_count"] = 1
        write_artifact(ctx, "m251a_news_lineage_report", "S03_m251a_news_raw_backfill", payload, row_count=0)
        raise RuntimeError("S03 news source backfill produced zero news/source rows for the configured period")
    if not idempotency_gate.get("passed"):
        payload["error_count"] = 1
        write_artifact(ctx, "m251a_news_lineage_report", "S03_m251a_news_raw_backfill", payload, row_count=payload["output_count"])
        raise RuntimeError("S03 idempotency gate failed; refusing to advance to M252/M253")
    write_artifact(ctx, "m251a_news_lineage_report", "S03_m251a_news_raw_backfill", payload, row_count=payload["output_count"])
    return payload


def stage_m251b_macro_event_backfill(ctx: BackfillContext) -> dict[str, Any]:
    start_date, end_date = _local_date_window(ctx)
    commands: list[dict[str, Any]] = []
    before_counts = _source_summary_counts(ctx)
    macro_source_input_counts = _macro_source_input_counts(ctx, start_date, end_date)
    macro_cfg = ctx.config.get("sources", {}).get("macro", {}) if isinstance(ctx.config.get("sources"), dict) else {}
    source_flags = macro_cfg.get("sources", {}) if isinstance(macro_cfg, dict) else {}
    skip_source_refresh_on_resume = bool(
        ctx.resume
        and _bool_config(ctx.config, "sources", "macro", "skip_source_refresh_on_resume", default=True)
        and (int(macro_source_input_counts.get("fred", 0)) + int(macro_source_input_counts.get("economic_indicators", 0)) > 0)
    )

    if skip_source_refresh_on_resume:
        commands.append(
            {
                "command": "macro_source_refresh",
                "status": "skipped",
                "reason": "macro source observations already present on resume; rebuilding local S04 lineage only",
                "source_input_counts": macro_source_input_counts,
            }
        )
    elif _bool_config(ctx.config, "sources", "macro", "enabled", default=True):
        fred_universes = _config_value(ctx.config, "macro", "fred_universes", default=["us_core", "global_core", "international_core"])
        if not isinstance(fred_universes, list):
            fred_universes = ["us_core", "global_core", "international_core"]
        if not isinstance(source_flags, dict) or bool(source_flags.get("fred", True)):
            for universe in fred_universes:
                commands.append(
                    run_main_command(
                        ctx,
                        "S04_m251b_macro_event_backfill",
                        f"fred_universe_sync_{universe}",
                        [
                            "fred-universe-sync",
                            "--universe",
                            str(universe),
                            "--start",
                            start_date,
                            "--end",
                            end_date,
                            "--limit-per-series",
                            str(_int_config(ctx.config, "sources", "macro", "limit_per_series", default=5000)),
                            "--build-macro-lineage",
                        ],
                        timeout_seconds=_command_timeout(ctx, f"fred_universe_sync_{universe}", 7200),
                    )
                )
        if not isinstance(source_flags, dict) or any(bool(source_flags.get(item, False)) for item in ("eurostat", "kosis", "bok", "boj", "ecb", "international")):
            commands.append(
                run_main_command(
                    ctx,
                    "S04_m251b_macro_event_backfill",
                    "international_macro_sync",
                    [
                        "international-macro-sync",
                        "--source",
                        "all",
                        "--start",
                        start_date,
                        "--end",
                        end_date,
                        "--build-macro-lineage",
                    ],
                    timeout_seconds=_command_timeout(ctx, "international_macro_sync", 7200),
                )
            )

    lineage_payload = rebuild_macro_event_lineage_from_observations(
        str(ctx.database_path),
        source="all",
        start=start_date,
        end=end_date,
        universe=str(ctx.config.get("macro", {}).get("universe", "global_core")),
    )
    after_counts = _source_summary_counts(ctx)
    macro_rows = int(after_counts.get("macro_event_releases", 0)) + int(after_counts.get("macro_observations", 0))
    payload = {
        "stage": "S04_m251b_macro_event_backfill",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "database_path": str(ctx.database_path),
        "start": start_date,
        "end": end_date,
        "commands": commands,
        "macro_source_input_counts": macro_source_input_counts,
        "skip_source_refresh_on_resume": skip_source_refresh_on_resume,
        "before_counts": before_counts,
        "after_counts": after_counts,
        "lineage": lineage_payload,
        "output_count": int(lineage_payload.get("macro_event_releases_total") or after_counts.get("macro_event_releases") or macro_rows),
        "macro_rows": macro_rows,
    }
    if _bool_config(ctx.config, "run", "require_non_empty_sources", default=True) and macro_rows <= 0:
        payload["error_count"] = 1
        write_artifact(ctx, "m251b_macro_lineage_report", "S04_m251b_macro_event_backfill", payload, row_count=0)
        raise RuntimeError("S04 macro source backfill produced zero macro rows for the configured period")
    write_artifact(ctx, "m251b_macro_lineage_report", "S04_m251b_macro_event_backfill", payload, row_count=payload["output_count"])
    return payload


def stage_count_only(ctx: BackfillContext, stage_name: str, artifact_type: str) -> dict[str, Any]:
    payload = {
        "stage": stage_name,
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "counts": _stage_summary_counts(ctx.database_path),
        "note": "This stage is satisfied by the current canonical lineage foundation and records an auditable checkpoint.",
        "output_count": 1,
    }
    write_artifact(ctx, artifact_type, stage_name, payload, row_count=1)
    return payload


def _reaction_config(ctx: BackfillContext) -> tuple[list[str], list[int]]:
    label_cfg = ctx.config.get("reaction_labels", {})
    windows = [str(v) for v in label_cfg.get("windows", ["5m", "15m", "1h", "close"])]
    lags = [int(v) for v in label_cfg.get("decision_lags_seconds", [0, 30, 60, 180])]
    return windows, lags


def stage_reaction_plan_build(ctx: BackfillContext) -> dict[str, Any]:
    payload = build_reaction_label_foundation(
        ctx.database_path,
        start=ctx.start_iso,
        end=ctx.end_iso,
        windows=(),
        decision_lags_seconds=(),
        create_label_stubs=False,
    )
    payload["stage"] = "S08_reaction_universe_plan_build"
    payload["run_id"] = ctx.run_id
    payload["build_id"] = ctx.build_id
    payload["config_hash"] = ctx.config_hash
    payload["output_count"] = int(payload.get("reaction_plans_touched") or 0)
    write_artifact(ctx, "reaction_plan_report", "S08_reaction_universe_plan_build", payload, row_count=payload["output_count"])
    return payload


def stage_market_data_cache_backfill(ctx: BackfillContext) -> dict[str, Any]:
    payload = {
        "stage": "S09_market_data_cache_backfill",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "status": "deferred",
        "note": "Market-data cache adapter is intentionally not implemented in this runner foundation. S10 creates pending-market-data label stubs only.",
        "warning_count": 1,
        "output_count": 0,
    }
    write_artifact(ctx, "market_data_cache_report", "S09_market_data_cache_backfill", payload)
    return payload


def stage_reaction_label_build(ctx: BackfillContext) -> dict[str, Any]:
    windows, lags = _reaction_config(ctx)
    payload = build_reaction_label_foundation(
        ctx.database_path,
        start=ctx.start_iso,
        end=ctx.end_iso,
        windows=windows,
        decision_lags_seconds=lags,
        create_label_stubs=True,
    )
    payload["stage"] = "S10_m253_reaction_label_build"
    payload["run_id"] = ctx.run_id
    payload["build_id"] = ctx.build_id
    payload["config_hash"] = ctx.config_hash
    payload["output_count"] = int(payload.get("reaction_labels_created") or 0)
    write_artifact(ctx, "reaction_label_report", "S10_m253_reaction_label_build", payload, row_count=payload["output_count"])
    return payload


def stage_reaction_label_audit(ctx: BackfillContext) -> dict[str, Any]:
    with connect(ctx.database_path) as conn:
        route_direct = 0
        fallback_direct = 0
        unresolved_direct = 0
        labels = _count(conn, "event_reaction_labels")
        pending = 0
        if _table_exists(conn, "event_reaction_labels"):
            pending = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM event_reaction_labels WHERE label_quality_flag='pending_market_data'"
                ).fetchone()["c"]
            )
        if _table_exists(conn, "reaction_universe_assets"):
            fallback_direct = int(
                conn.execute(
                    """SELECT COUNT(*) AS c
                         FROM reaction_universe_assets
                        WHERE label_scope='direct'
                          AND inclusion_source IN ('fallback_context','ibkr_route')"""
                ).fetchone()["c"]
            )
        if _table_exists(conn, "news_asset_candidates"):
            unresolved_direct = int(
                conn.execute(
                    """SELECT COUNT(*) AS c
                         FROM news_asset_candidates
                        WHERE role IN ('primary','secondary')
                          AND tradable_candidate=1
                          AND resolver_status!='resolved'"""
                ).fetchone()["c"]
            )
    criticals = {
        "route_only_direct_label_count": route_direct,
        "fallback_context_direct_label_count": fallback_direct,
        "unresolved_tradable_candidate_count": unresolved_direct,
    }
    critical_count = sum(1 for value in criticals.values() if int(value) > 0)
    payload = {
        "stage": "S11_reaction_label_audit",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "event_reaction_labels": labels,
        "pending_market_data_labels": pending,
        "critical_counts": criticals,
        "critical_violation_count": critical_count,
        "warning_count": 1 if pending else 0,
        "error_count": critical_count,
        "output_count": labels,
    }
    write_artifact(ctx, "reaction_label_audit", "S11_reaction_label_audit", payload, row_count=labels)
    if critical_count:
        raise RuntimeError(f"reaction label audit failed: {criticals}")
    return payload


def stage_replay_features_build(ctx: BackfillContext) -> dict[str, Any]:
    now = utc_now_iso()
    inserted = 0
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        rows = conn.execute(
            """SELECT c.event_cluster_id,c.cluster_key,c.primary_headline,c.primary_event_name,
                      c.event_class,c.first_seen_at_utc,c.last_seen_at_utc,
                      p.reaction_plan_id,p.available_at_cutoff
                 FROM news_event_clusters c
                 JOIN reaction_universe_plans p ON p.event_cluster_id=c.event_cluster_id
                WHERE COALESCE(NULLIF(c.first_seen_at_utc,''),c.created_at) >= ?
                  AND COALESCE(NULLIF(c.first_seen_at_utc,''),c.created_at) <= ?
                ORDER BY c.event_cluster_id""",
            (ctx.start_iso, ctx.end_iso),
        ).fetchall()
        before = _count(conn, "replay_features")
        for row in rows:
            assets = [
                dict(asset)
                for asset in conn.execute(
                    """SELECT asset_symbol,asset_role,proxy_family,label_scope,inclusion_source
                         FROM reaction_universe_assets
                        WHERE reaction_plan_id=?
                        ORDER BY label_scope,asset_symbol""",
                    (int(row["reaction_plan_id"]),),
                ).fetchall()
            ]
            replay_id = f"{ctx.run_id}:cluster:{int(row['event_cluster_id'])}"
            model_visible = str(row["available_at_cutoff"] or row["first_seen_at_utc"] or row["last_seen_at_utc"] or "")
            snapshot = {
                "run_id": ctx.run_id,
                "build_id": ctx.build_id,
                "config_hash": ctx.config_hash,
                "event_cluster_id": int(row["event_cluster_id"]),
                "cluster_key": row["cluster_key"],
                "headline": row["primary_headline"],
                "primary_event_name": row["primary_event_name"],
                "event_class": row["event_class"],
                "planned_assets": assets,
                "point_in_time_note": "No event_reaction_labels or future outcomes are included in replay_features.",
            }
            conn.execute(
                """INSERT OR IGNORE INTO replay_features(
                       replay_id,event_cluster_id,as_of_time,model_visible_at,feature_snapshot_json,
                       feature_version,proxy_map_version,security_master_version,mapper_version,
                       classifier_version,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    replay_id,
                    int(row["event_cluster_id"]),
                    model_visible,
                    model_visible,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    REPLAY_FEATURE_VERSION,
                    EVENT_PROXY_MAP_VERSION,
                    SECURITY_MASTER_VERSION,
                    NEWS_ASSET_MAPPER_VERSION,
                    NEWS_EVENT_CLUSTERER_VERSION,
                    now,
                ),
            )
        after = _count(conn, "replay_features")
        inserted = max(0, after - before)
    payload = {
        "stage": "S12_m254_replay_features_build",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "replay_features_created": inserted,
        "output_count": inserted,
    }
    write_artifact(ctx, "replay_features_report", "S12_m254_replay_features_build", payload, row_count=inserted)
    return payload


def stage_replay_outcomes_build(ctx: BackfillContext) -> dict[str, Any]:
    now = utc_now_iso()
    inserted = 0
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        before = _count(conn, "replay_outcomes")
        rows = conn.execute(
            """SELECT f.replay_id,l.window,l.asset_symbol,l.raw_return,l.label_quality_flag
                 FROM replay_features f
                 JOIN event_reaction_labels l ON l.event_cluster_id=f.event_cluster_id
                WHERE f.replay_id LIKE ?
                ORDER BY f.replay_id,l.window,l.asset_symbol""",
            (f"{ctx.run_id}:%",),
        ).fetchall()
        for row in rows:
            label = str(row["label_quality_flag"] or "pending_market_data")
            conn.execute(
                """INSERT OR IGNORE INTO replay_outcomes(
                       replay_id,outcome_window,asset_symbol,outcome_label,pnl_label,
                       outcome_builder_version,created_at
                   ) VALUES (?,?,?,?,?,?,?)""",
                (
                    row["replay_id"],
                    row["window"],
                    row["asset_symbol"],
                    label,
                    row["raw_return"],
                    REPLAY_OUTCOME_VERSION,
                    now,
                ),
            )
        after = _count(conn, "replay_outcomes")
        inserted = max(0, after - before)
    payload = {
        "stage": "S13_m254_replay_outcomes_build",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "replay_outcomes_created": inserted,
        "output_count": inserted,
    }
    write_artifact(ctx, "replay_outcomes_report", "S13_m254_replay_outcomes_build", payload, row_count=inserted)
    return payload


def stage_replay_leakage_audit(ctx: BackfillContext) -> dict[str, Any]:
    with connect(ctx.database_path) as conn:
        ensure_run_control_schema(conn)
        missing_visible = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM replay_features WHERE replay_id LIKE ? AND model_visible_at=''",
                (f"{ctx.run_id}:%",),
            ).fetchone()["c"]
        )
        outcome_words_in_features = 0
        for row in conn.execute(
            "SELECT feature_snapshot_json FROM replay_features WHERE replay_id LIKE ? LIMIT 1000",
            (f"{ctx.run_id}:%",),
        ).fetchall():
            text = str(row["feature_snapshot_json"] or "").lower()
            if any(token in text for token in ("raw_return", "mfe", "mae", "pnl_label", "outcome_label")):
                outcome_words_in_features += 1
    critical_count = int(missing_visible > 0) + int(outcome_words_in_features > 0)
    payload = {
        "stage": "S14_replay_leakage_audit",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "missing_model_visible_at": missing_visible,
        "feature_rows_with_outcome_terms": outcome_words_in_features,
        "critical_violation_count": critical_count,
        "error_count": critical_count,
        "output_count": 1,
    }
    write_artifact(ctx, "replay_leakage_audit", "S14_replay_leakage_audit", payload, row_count=1)
    if critical_count:
        raise RuntimeError(f"replay leakage audit failed: {payload}")
    return payload


def stage_deferred_validation(ctx: BackfillContext, stage_name: str) -> dict[str, Any]:
    payload = {
        "stage": stage_name,
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "status": "blocked_until_prior_canonical_dataset_has_real_labels",
        "note": "M255/M256 are intentionally locked in this runner foundation. Build real reaction labels and pass audits before validation/calibration/blind cycles.",
        "warning_count": 1,
        "output_count": 0,
    }
    write_artifact(ctx, "deferred_validation_report", stage_name, payload)
    return payload


def stage_final_report(ctx: BackfillContext) -> dict[str, Any]:
    with connect(ctx.database_path) as conn:
        stages = [
            dict(row)
            for row in conn.execute(
                """SELECT stage_name,status,output_count,warning_count,error_count,started_at,completed_at,error_message
                     FROM research_backfill_stage_runs
                    WHERE run_id=?
                    ORDER BY stage_run_id""",
                (ctx.run_id,),
            ).fetchall()
        ]
        artifacts = [
            dict(row)
            for row in conn.execute(
                "SELECT artifact_type,path,row_count,checksum,created_at FROM research_build_artifacts WHERE run_id=? ORDER BY artifact_id",
                (ctx.run_id,),
            ).fetchall()
        ]
    payload = {
        "stage": "S21_final_report",
        "run_id": ctx.run_id,
        "build_id": ctx.build_id,
        "config_hash": ctx.config_hash,
        "counts": _stage_summary_counts(ctx.database_path),
        "stages": stages,
        "artifacts": artifacts,
        "output_count": len(stages),
    }
    write_artifact(ctx, "final_report", "S21_final_report", payload, row_count=len(stages))
    md_path = ctx.output_dir / f"{ctx.run_id}_S21_final_report.md"
    lines = [
        "# Canonical Research Backfill Final Report",
        "",
        f"- Run ID: `{ctx.run_id}`",
        f"- Build ID: `{ctx.build_id}`",
        f"- Config hash: `{ctx.config_hash}`",
        f"- Window UTC: `{ctx.start_iso}` -> `{ctx.end_iso}`",
        "",
        "## Stage status",
        "",
    ]
    for stage in stages:
        lines.append(
            f"- {stage['stage_name']}: {stage['status']} "
            f"(output={stage['output_count']}, warnings={stage['warning_count']}, errors={stage['error_count']})"
        )
    lines.extend(["", "## Counts", ""])
    for key, value in payload["counts"].items():
        lines.append(f"- {key}: {value}")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    record_artifact(ctx, "final_report_markdown", md_path, row_count=len(stages))
    return payload


def db_audit_error_counts(stdout: str, returncode: int) -> dict[str, int]:
    if returncode:
        return {"critical": 0, "error": 1, "total": 1}
    try:
        payload = json.loads(stdout or "{}")
    except json.JSONDecodeError:
        return {"critical": 0, "error": 0, "total": 0}
    severity_counts = payload.get("severity_counts") or {}
    critical = int(severity_counts.get("critical") or 0)
    error = int(severity_counts.get("error") or 0)
    return {"critical": critical, "error": error, "total": critical + error}


def stage_db_audit_post(ctx: BackfillContext) -> dict[str, Any]:
    result: dict[str, Any]
    completed = subprocess.run(
        [sys.executable, "main.py", "db-audit"],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        check=False,
        env=_subprocess_env(ctx),
    )
    audit_counts = db_audit_error_counts(completed.stdout, completed.returncode)
    result = {
        "stage": "S22_db_audit_post",
        "returncode": completed.returncode,
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
        "output_count": 1,
        "audit_critical_count": audit_counts["critical"],
        "audit_error_count": audit_counts["error"],
        "error_count": audit_counts["total"],
    }
    write_artifact(ctx, "db_audit_post", "S22_db_audit_post", result, row_count=1)
    if audit_counts["total"]:
        raise RuntimeError(
            "db-audit failed: "
            f"returncode={completed.returncode}, "
            f"critical={audit_counts['critical']}, "
            f"errors={audit_counts['error']}"
        )
    return result


def build_stages() -> list[Stage]:
    return [
        Stage("S00_preflight", stage_preflight),
        Stage("S01_schema_migration", stage_schema_migration, requires=("S00_preflight",)),
        Stage("S02_build_manifest", stage_build_manifest, requires=("S01_schema_migration",)),
        Stage("S03_m251a_news_raw_backfill", stage_m251a_news_raw_backfill, requires=("S02_build_manifest",)),
        Stage("S04_m251b_macro_event_backfill", stage_m251b_macro_event_backfill, requires=("S02_build_manifest",)),
        Stage("S05_article_enrichment_and_asset_mapping", lambda ctx: stage_count_only(ctx, "S05_article_enrichment_and_asset_mapping", "asset_mapping_checkpoint"), requires=("S03_m251a_news_raw_backfill",)),
        Stage("S06_m252_event_cluster_build", lambda ctx: stage_count_only(ctx, "S06_m252_event_cluster_build", "event_cluster_checkpoint"), requires=("S03_m251a_news_raw_backfill", "S04_m251b_macro_event_backfill")),
        Stage("S07_event_classification_and_gate", lambda ctx: stage_count_only(ctx, "S07_event_classification_and_gate", "event_gate_checkpoint"), requires=("S06_m252_event_cluster_build",)),
        Stage("S08_reaction_universe_plan_build", stage_reaction_plan_build, requires=("S07_event_classification_and_gate",)),
        Stage("S09_market_data_cache_backfill", stage_market_data_cache_backfill, requires=("S08_reaction_universe_plan_build",)),
        Stage("S10_m253_reaction_label_build", stage_reaction_label_build, requires=("S08_reaction_universe_plan_build", "S09_market_data_cache_backfill")),
        Stage("S11_reaction_label_audit", stage_reaction_label_audit, requires=("S10_m253_reaction_label_build",), audit_gate=True),
        Stage("S12_m254_replay_features_build", stage_replay_features_build, requires=("S11_reaction_label_audit",)),
        Stage("S13_m254_replay_outcomes_build", stage_replay_outcomes_build, requires=("S12_m254_replay_features_build",)),
        Stage("S14_replay_leakage_audit", stage_replay_leakage_audit, requires=("S13_m254_replay_outcomes_build",), audit_gate=True),
        Stage("S15_m255_baselines", lambda ctx: stage_deferred_validation(ctx, "S15_m255_baselines"), requires=("S14_replay_leakage_audit",)),
        Stage("S16_m255_semantic_scorer", lambda ctx: stage_deferred_validation(ctx, "S16_m255_semantic_scorer"), requires=("S15_m255_baselines",)),
        Stage("S17_m255_reaction_learner", lambda ctx: stage_deferred_validation(ctx, "S17_m255_reaction_learner"), requires=("S16_m255_semantic_scorer",)),
        Stage("S18_m255_policy_scorer", lambda ctx: stage_deferred_validation(ctx, "S18_m255_policy_scorer"), requires=("S17_m255_reaction_learner",)),
        Stage("S19_m255_walk_forward_validation", lambda ctx: stage_deferred_validation(ctx, "S19_m255_walk_forward_validation"), requires=("S18_m255_policy_scorer",)),
        Stage("S20_m256_blind_cycle", lambda ctx: stage_deferred_validation(ctx, "S20_m256_blind_cycle"), requires=("S19_m255_walk_forward_validation",), blind_stage=True),
        Stage("S21_final_report", stage_final_report, requires=("S14_replay_leakage_audit",)),
        Stage("S22_db_audit_post", stage_db_audit_post, requires=("S21_final_report",)),
    ]


def _validate_dependencies(ctx: BackfillContext, stage: Stage) -> None:
    missing = [name for name in stage.requires if not stage_completed(ctx, name)]
    if missing:
        raise RuntimeError(f"{stage.name} requires completed stages: {', '.join(missing)}")


def run_stage(ctx: BackfillContext, stage: Stage) -> dict[str, Any]:
    if stage.blind_stage and not ctx.allow_blind:
        payload = {
            "stage": stage.name,
            "status": "skipped_locked_blind_stage",
            "reason": "Pass --allow-blind to run final blind/calibration stages.",
            "output_count": 0,
            "warning_count": 1,
        }
        write_artifact(ctx, "blind_stage_locked", stage.name, payload)
        with connect(ctx.database_path) as conn:
            ensure_run_control_schema(conn)
            conn.execute(
                """INSERT OR REPLACE INTO research_backfill_stage_runs(
                       run_id,stage_name,stage_version,status,input_watermark_start,input_watermark_end,
                       output_count,warning_count,error_count,started_at,completed_at,error_message,payload_json
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ctx.run_id,
                    stage.name,
                    stage.version,
                    "skipped",
                    ctx.start_iso,
                    ctx.end_iso,
                    0,
                    1,
                    0,
                    utc_now_iso(),
                    utc_now_iso(),
                    "",
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
        return payload
    if ctx.resume and not ctx.force_stage and stage_completed(ctx, stage.name):
        return {"stage": stage.name, "status": "skipped_completed", "output_count": 0}
    _validate_dependencies(ctx, stage)
    mark_stage_running(ctx, stage)
    try:
        payload = stage.fn(ctx)
        mark_stage_completed(ctx, stage, payload)
        return payload
    except BaseException as exc:
        mark_stage_failed(ctx, stage, exc)
        raise


def build_context(args: argparse.Namespace) -> BackfillContext:
    config_path = Path(args.config)
    config, config_hash = _read_config(config_path)
    run_cfg = config.get("run", {})
    timezone_name = str(run_cfg.get("timezone", "America/New_York"))
    start_utc = _parse_local_datetime(str(run_cfg["start_local"]), timezone_name)
    end_utc = _parse_local_datetime(str(run_cfg["end_local"]), timezone_name)
    settings = Settings.from_env()
    run_id = str(args.run_id or run_cfg.get("run_id") or f"canonical_research_{config_hash}")
    build_id = str(run_cfg.get("build_id") or run_id)
    output_dir = Path(str(run_cfg.get("output_dir") or "exports/research_backfill")) / run_id
    configured_db = str(run_cfg.get("database_path") or "").strip()
    database_path = Path(configured_db.format(run_id=run_id, build_id=build_id)) if configured_db else Path(settings.database_path)
    if not database_path.is_absolute():
        database_path = Path.cwd() / database_path
    database_path.parent.mkdir(parents=True, exist_ok=True)
    return BackfillContext(
        run_id=run_id,
        build_id=build_id,
        config_path=config_path,
        config=config,
        config_hash=config_hash,
        database_path=database_path,
        start_utc=start_utc,
        end_utc=end_utc,
        timezone_name=timezone_name,
        resume=bool(args.resume or run_cfg.get("resume", False)),
        force_stage=bool(args.force_stage),
        allow_blind=bool(args.allow_blind),
        dry_run=bool(args.dry_run or run_cfg.get("dry_run", False)),
        output_dir=output_dir,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Canonical research backfill runner")
    parser.add_argument("--config", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--stage", help="Run one stage by exact stage name or short token such as preflight")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--force-stage", action="store_true")
    parser.add_argument("--allow-blind", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--list-stages", action="store_true")
    args = parser.parse_args(argv)

    stages = build_stages()
    if args.list_stages:
        for stage in stages:
            print(stage.name)
        return 0

    ctx = build_context(args)
    register_or_resume_run(ctx)

    selected = stages
    if args.stage:
        token = str(args.stage).lower()
        aliases = {
            "preflight": "S00_preflight",
            "audit": "S11_reaction_label_audit",
            "m251a_news": "S03_m251a_news_raw_backfill",
            "m251b_macro": "S04_m251b_macro_event_backfill",
            "m253_reaction_labels": "S10_m253_reaction_label_build",
            "m254_features": "S12_m254_replay_features_build",
            "m254_outcomes": "S13_m254_replay_outcomes_build",
            "m256_blind_cycle": "S20_m256_blind_cycle",
        }
        wanted = aliases.get(token, args.stage)
        selected = [stage for stage in stages if stage.name == wanted]
        if not selected:
            raise SystemExit(f"Unknown stage: {args.stage}")

    if ctx.dry_run:
        print(
            json.dumps(
                {
                    "run_id": ctx.run_id,
                    "config_hash": ctx.config_hash,
                    "database_path": str(ctx.database_path),
                    "start_utc": ctx.start_iso,
                    "end_utc": ctx.end_iso,
                    "selected_stages": [stage.name for stage in selected],
                    "dry_run": True,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    results = []
    for stage in selected:
        result = run_stage(ctx, stage)
        results.append(result)
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
    finalise_run_if_possible(ctx, stages)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
