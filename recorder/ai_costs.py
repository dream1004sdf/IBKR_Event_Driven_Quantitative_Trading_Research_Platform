from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


AI_COST_REPORT_VERSION = "ai-cost-report-v4-billing-payer-aware"
METERED_TOKEN_TABLES = ("news_analyses", "replay_analyses", "m223_live_learning_adjustment_notes")
METERED_TOKEN_TABLE_SPECS = {
    "news_analyses": {
        "provider_col": "provider",
        "model_col": "model_name",
        "date_preference": ("completed_at", "created_at"),
    },
    "replay_analyses": {
        "provider_col": "provider_name",
        "model_col": "model_name",
        "date_preference": ("completed_at", "created_at"),
    },
    "m223_live_learning_adjustment_notes": {
        "provider_col": "analysis_provider",
        "model_col": "analysis_model",
        "date_preference": ("created_at",),
    },
}
MODEL_ONLY_USAGE_TABLES = (
    ("m223_live_learning_adjustment_notes", "analysis_provider", "analysis_model", "created_at"),
    ("replay_surprise_adjustment_notes", "analysis_provider", "analysis_model", "created_at"),
)


def _read_pricing(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {"currency": "USD", "models": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"currency": "USD", "models": {}}
    return payload if isinstance(payload, dict) else {"currency": "USD", "models": {}}


def _price_for_model(pricing: dict[str, Any], model_name: str) -> dict[str, Any]:
    models = pricing.get("models") if isinstance(pricing.get("models"), dict) else {}
    direct = models.get(model_name) if isinstance(models.get(model_name), dict) else None
    if direct is not None:
        return direct
    lower = str(model_name or "").lower()
    for key, value in models.items():
        if str(key).lower() == lower and isinstance(value, dict):
            return value
    return {}


def _estimate_cost(input_tokens: int, output_tokens: int, price: dict[str, Any], *, prefix: str = "") -> tuple[float | None, str]:
    if int(input_tokens or 0) == 0 and int(output_tokens or 0) == 0:
        return 0.0, "no_billable_tokens"
    try:
        input_price = float(price.get(f"{prefix}input_per_1m"))
        output_price = float(price.get(f"{prefix}output_per_1m"))
    except (TypeError, ValueError):
        return None, "missing_price"
    return (input_tokens / 1_000_000.0) * input_price + (output_tokens / 1_000_000.0) * output_price, "estimated"


def _billable_cost_fields(
    *,
    input_tokens: int,
    output_tokens: int,
    price: dict[str, Any],
    billing_payer: str,
    prefix: str = "",
) -> dict[str, Any]:
    usage_equivalent_cost, usage_status = _estimate_cost(input_tokens, output_tokens, price, prefix=prefix)
    payer = str(billing_payer or "unknown").strip().lower() or "unknown"
    if payer == "openai":
        return {
            "estimated_cost": 0.0,
            "cost_status": "non_billable_openai_payer",
            "usage_equivalent_estimated_cost": round(usage_equivalent_cost, 6) if usage_equivalent_cost is not None else None,
            "usage_equivalent_cost_status": usage_status,
        }
    return {
        "estimated_cost": round(usage_equivalent_cost, 6) if usage_equivalent_cost is not None else None,
        "cost_status": usage_status,
        "usage_equivalent_estimated_cost": round(usage_equivalent_cost, 6) if usage_equivalent_cost is not None else None,
        "usage_equivalent_cost_status": usage_status,
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    columns = _columns(conn, table)
    spec = METERED_TOKEN_TABLE_SPECS.get(table) or {}
    provider_col = str(spec.get("provider_col") or "provider")
    model_col = str(spec.get("model_col") or "model_name")
    date_candidates = tuple(spec.get("date_preference") or ("completed_at", "created_at"))
    required = {model_col, "input_tokens", "output_tokens"}
    if not required.issubset(columns):
        return []
    date_col = next((candidate for candidate in date_candidates if candidate in columns), "created_at")
    if date_col not in columns:
        return []
    provider_expr = provider_col if provider_col in columns else "'unknown'"
    billing_payer_expr = "billing_payer" if "billing_payer" in columns else "'unknown'"
    token_where = "COALESCE(input_tokens, 0) <> 0 OR COALESCE(output_tokens, 0) <> 0"
    return [
        {
            "source_table": table,
            "day": str(row[0] or "")[:10] or "unknown",
            "provider": row[1] or "unknown",
            "model_name": row[2] or "unknown",
            "billing_payer": row[3] or "unknown",
            "row_count": int(row[4] or 0),
            "input_tokens": int(row[5] or 0),
            "output_tokens": int(row[6] or 0),
        }
        for row in conn.execute(
            f"""
            SELECT substr(COALESCE({date_col}, ''), 1, 10) AS day,
                   {provider_expr} AS provider,
                   {model_col} AS model_name,
                   COALESCE(NULLIF({billing_payer_expr}, ''), 'unknown') AS billing_payer,
                   COUNT(*) AS row_count,
                   SUM(COALESCE(input_tokens, 0)) AS input_tokens,
                   SUM(COALESCE(output_tokens, 0)) AS output_tokens
            FROM {table}
            WHERE {token_where}
            GROUP BY day, provider, model_name, billing_payer
            ORDER BY day DESC, provider, model_name, billing_payer
            """
        )
    ]


def _unmetered_model_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for table, provider_col, model_col, created_col in MODEL_ONLY_USAGE_TABLES:
        columns = _columns(conn, table)
        if not {provider_col, model_col, created_col}.issubset(columns):
            continue
        token_filter = ""
        if {"input_tokens", "output_tokens"}.issubset(columns):
            token_filter = "WHERE COALESCE(input_tokens, 0) = 0 AND COALESCE(output_tokens, 0) = 0"
        for row in conn.execute(
            f"""
            SELECT substr(COALESCE({created_col}, ''), 1, 10) AS day,
                   COALESCE(NULLIF({provider_col}, ''), 'unknown') AS provider,
                   COALESCE(NULLIF({model_col}, ''), 'unknown') AS model_name,
                   COUNT(*) AS row_count
            FROM {table}
            {token_filter}
            GROUP BY day, provider, model_name
            ORDER BY day DESC, provider, model_name
            """
        ):
            rows.append(
                {
                    "source_table": table,
                    "day": str(row[0] or "")[:10] or "unknown",
                    "provider": row[1] or "unknown",
                    "model_name": row[2] or "unknown",
                    "row_count": int(row[3] or 0),
                    "coverage_status": "model_recorded_tokens_missing",
                }
            )
    return rows


def build_ai_cost_report(
    *,
    database_path: str | Path,
    pricing_path: str | Path | None = Path("config/ai_cost_pricing_v1.json"),
) -> dict[str, Any]:
    pricing_file = Path(pricing_path) if pricing_path is not None else None
    pricing = _read_pricing(pricing_file)
    currency = str(pricing.get("currency") or "USD")
    conn = sqlite3.connect(database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = [item for table in METERED_TOKEN_TABLES for item in _rows(conn, table)]
        unmetered_rows = _unmetered_model_rows(conn)
    finally:
        conn.close()

    by_day_model: list[dict[str, Any]] = []
    totals_by_model: dict[tuple[str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"row_count": 0, "input_tokens": 0, "output_tokens": 0}
    )
    total_estimated_cost = 0.0
    total_estimated_cost_available = True
    total_usage_equivalent_cost = 0.0
    total_usage_equivalent_cost_available = True
    for row in rows:
        price = _price_for_model(pricing, str(row["model_name"]))
        cost_fields = _billable_cost_fields(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            price=price,
            billing_payer=str(row.get("billing_payer") or "unknown"),
        )
        batch_cost_fields = _billable_cost_fields(
            input_tokens=int(row["input_tokens"]),
            output_tokens=int(row["output_tokens"]),
            price=price,
            billing_payer=str(row.get("billing_payer") or "unknown"),
            prefix="batch_",
        )
        estimated_cost = cost_fields["estimated_cost"]
        usage_equivalent_cost = cost_fields["usage_equivalent_estimated_cost"]
        if estimated_cost is None:
            total_estimated_cost_available = False
        else:
            total_estimated_cost += float(estimated_cost)
        if usage_equivalent_cost is None:
            total_usage_equivalent_cost_available = False
        else:
            total_usage_equivalent_cost += float(usage_equivalent_cost)
        item = {
            **row,
            "total_tokens": int(row["input_tokens"]) + int(row["output_tokens"]),
            "currency": currency,
            "estimated_cost": estimated_cost,
            "cost_status": cost_fields["cost_status"],
            "usage_equivalent_estimated_cost": usage_equivalent_cost,
            "usage_equivalent_cost_status": cost_fields["usage_equivalent_cost_status"],
            "batch_estimated_cost": batch_cost_fields["estimated_cost"],
            "batch_cost_status": batch_cost_fields["cost_status"],
            "batch_usage_equivalent_estimated_cost": batch_cost_fields["usage_equivalent_estimated_cost"],
            "batch_usage_equivalent_cost_status": batch_cost_fields["usage_equivalent_cost_status"],
            "input_per_1m": price.get("input_per_1m"),
            "cached_input_per_1m": price.get("cached_input_per_1m"),
            "output_per_1m": price.get("output_per_1m"),
            "batch_input_per_1m": price.get("batch_input_per_1m"),
            "batch_cached_input_per_1m": price.get("batch_cached_input_per_1m"),
            "batch_output_per_1m": price.get("batch_output_per_1m"),
        }
        by_day_model.append(item)
        key = (str(row["provider"]), str(row["model_name"]), str(row.get("billing_payer") or "unknown"))
        bucket = totals_by_model[key]
        bucket["provider"], bucket["model_name"], bucket["billing_payer"] = key
        bucket["row_count"] += int(row["row_count"])
        bucket["input_tokens"] += int(row["input_tokens"])
        bucket["output_tokens"] += int(row["output_tokens"])

    model_totals: list[dict[str, Any]] = []
    for (_provider, model_name, billing_payer), bucket in sorted(totals_by_model.items()):
        price = _price_for_model(pricing, model_name)
        cost_fields = _billable_cost_fields(
            input_tokens=int(bucket["input_tokens"]),
            output_tokens=int(bucket["output_tokens"]),
            price=price,
            billing_payer=billing_payer,
        )
        batch_cost_fields = _billable_cost_fields(
            input_tokens=int(bucket["input_tokens"]),
            output_tokens=int(bucket["output_tokens"]),
            price=price,
            billing_payer=billing_payer,
            prefix="batch_",
        )
        model_totals.append(
            {
                **bucket,
                "total_tokens": int(bucket["input_tokens"]) + int(bucket["output_tokens"]),
                "currency": currency,
                "estimated_cost": cost_fields["estimated_cost"],
                "cost_status": cost_fields["cost_status"],
                "usage_equivalent_estimated_cost": cost_fields["usage_equivalent_estimated_cost"],
                "usage_equivalent_cost_status": cost_fields["usage_equivalent_cost_status"],
                "batch_estimated_cost": batch_cost_fields["estimated_cost"],
                "batch_cost_status": batch_cost_fields["cost_status"],
                "batch_usage_equivalent_estimated_cost": batch_cost_fields["usage_equivalent_estimated_cost"],
                "batch_usage_equivalent_cost_status": batch_cost_fields["usage_equivalent_cost_status"],
                "input_per_1m": price.get("input_per_1m"),
                "cached_input_per_1m": price.get("cached_input_per_1m"),
                "output_per_1m": price.get("output_per_1m"),
                "batch_input_per_1m": price.get("batch_input_per_1m"),
                "batch_cached_input_per_1m": price.get("batch_cached_input_per_1m"),
                "batch_output_per_1m": price.get("batch_output_per_1m"),
            }
        )

    return {
        "ai_cost_report_version": AI_COST_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "database_path": str(Path(database_path).expanduser().resolve()),
        "pricing_path": str(pricing_file.expanduser().resolve()) if pricing_file else None,
        "pricing_version": pricing.get("pricing_version"),
        "pricing_source_url": pricing.get("source_url"),
        "pricing_checked_at": pricing.get("checked_at"),
        "currency": currency,
        "source_tables": list(METERED_TOKEN_TABLES),
        "metered_source_tables": list(METERED_TOKEN_TABLES),
        "unmetered_source_tables": sorted({row["source_table"] for row in unmetered_rows}),
        "row_count": sum(int(row["row_count"]) for row in rows),
        "unmetered_row_count": sum(int(row["row_count"]) for row in unmetered_rows),
        "input_tokens": sum(int(row["input_tokens"]) for row in rows),
        "output_tokens": sum(int(row["output_tokens"]) for row in rows),
        "total_tokens": sum(int(row["input_tokens"]) + int(row["output_tokens"]) for row in rows),
        "estimated_cost": round(total_estimated_cost, 6) if total_estimated_cost_available else None,
        "cost_status": "estimated" if total_estimated_cost_available else "missing_price",
        "usage_equivalent_estimated_cost": round(total_usage_equivalent_cost, 6) if total_usage_equivalent_cost_available else None,
        "usage_equivalent_cost_status": "estimated" if total_usage_equivalent_cost_available else "missing_price",
        "billing_note": "Rows with billing_payer='openai' consumed model tokens but are not estimated as developer-billable cost.",
        "model_totals": model_totals,
        "daily_model_rows": by_day_model,
        "unmetered_model_rows": unmetered_rows,
        "coverage_warning": (
            "Rows in unmetered_model_rows used an LLM/model path but do not store input/output token counts yet; "
            "cost is therefore a lower-bound estimate until those producers persist usage."
            if unmetered_rows
            else ""
        ),
        "pricing_warning": "Review config/ai_cost_pricing_v1.json when provider pricing changes. Cached-token discounts are stored but not applied unless cached-token usage is recorded.",
    }


def ai_cost_report_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# AI Cost Report",
        "",
        f"- Version: `{report.get('ai_cost_report_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Total rows: `{report.get('row_count')}`",
        f"- Unmetered model rows: `{report.get('unmetered_row_count')}`",
        f"- Input tokens: `{report.get('input_tokens')}`",
        f"- Output tokens: `{report.get('output_tokens')}`",
        f"- Total tokens: `{report.get('total_tokens')}`",
        f"- Developer-billable estimated cost: `{report.get('estimated_cost')}` {report.get('currency') or ''} ({report.get('cost_status')})",
        f"- Usage-equivalent estimated cost: `{report.get('usage_equivalent_estimated_cost')}` {report.get('currency') or ''} ({report.get('usage_equivalent_cost_status')})",
        f"- Billing note: {report.get('billing_note') or '-'}",
        f"- Pricing source: `{report.get('pricing_source_url') or '-'}` checked `{report.get('pricing_checked_at') or '-'}`",
        f"- Pricing warning: {report.get('pricing_warning')}",
        f"- Coverage warning: {report.get('coverage_warning') or '-'}",
        "",
        "## Model totals",
        "",
        "| Provider | Model | Payer | Rows | Input | Output | Total | Billable cost | Usage-equivalent | Status |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in report.get("model_totals") or []:
        lines.append(
            f"| {row.get('provider')} | `{row.get('model_name')}` | `{row.get('billing_payer') or 'unknown'}` | {row.get('row_count')} | "
            f"{row.get('input_tokens')} | {row.get('output_tokens')} | {row.get('total_tokens')} | "
            f"{row.get('estimated_cost')} | {row.get('usage_equivalent_estimated_cost')} | {row.get('cost_status')} |"
        )
    lines.extend(["", "## Daily model rows", "", "| Day | Provider | Model | Payer | Rows | Input | Output | Billable cost | Usage-equivalent | Status |", "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- |"])
    for row in report.get("daily_model_rows") or []:
        lines.append(
            f"| {row.get('day')} | {row.get('provider')} | `{row.get('model_name')}` | `{row.get('billing_payer') or 'unknown'}` | {row.get('row_count')} | "
            f"{row.get('input_tokens')} | {row.get('output_tokens')} | {row.get('estimated_cost')} | {row.get('usage_equivalent_estimated_cost')} | {row.get('cost_status')} |"
        )
    lines.extend(["", "## Unmetered model rows", "", "| Day | Table | Provider | Model | Rows | Coverage |", "| --- | --- | --- | --- | ---: | --- |"])
    for row in report.get("unmetered_model_rows") or []:
        lines.append(
            f"| {row.get('day')} | `{row.get('source_table')}` | {row.get('provider')} | `{row.get('model_name')}` | "
            f"{row.get('row_count')} | {row.get('coverage_status')} |"
        )
    return "\n".join(lines)


def write_ai_cost_report_json(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def write_ai_cost_report_markdown(output_path: Path, report: dict[str, Any]) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(ai_cost_report_markdown(report), encoding="utf-8")
    return output_path
