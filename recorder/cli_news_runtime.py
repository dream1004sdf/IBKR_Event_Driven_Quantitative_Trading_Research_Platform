from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recorder.config import Settings
from recorder.database import Database
from .cli_profile_utils import (
    coalesce_profile_path as _coalesce_profile_path,
    coalesce_profile_value as _coalesce_profile_value,
    load_profile_json as _load_profile_json,
)


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def command_ibkr_news_backfill(
    settings: Settings,
    symbols: str | None,
    providers: str | None,
    start: str,
    end: str,
    limit_per_symbol: int,
    fetch_article_body: bool,
    ibkr_client_id: int | None,
    timeout: float,
    max_pages_per_symbol: int = 1,
) -> None:
    from recorder.ibkr_historical_news import (
        IBKRHistoricalNewsBackfiller,
        parse_cli_datetime,
    )

    selected_symbols = tuple(
        item.strip().upper()
        for item in (symbols.split(",") if symbols else settings.watch_symbols)
        if item.strip()
    )
    provider_codes = (
        tuple(item.strip().upper() for item in providers.split(",") if item.strip())
        if providers
        else None
    )
    backfiller = IBKRHistoricalNewsBackfiller(
        settings,
        symbols=selected_symbols,
        start=parse_cli_datetime(start),
        end=parse_cli_datetime(end, end_of_day=True),
        provider_codes=provider_codes,
        limit_per_symbol=limit_per_symbol,
        fetch_article_body=fetch_article_body,
        client_id=ibkr_client_id,
        timeout_seconds=timeout,
        max_pages_per_symbol=max_pages_per_symbol,
    )
    result = backfiller.run_backfill()
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))


def _recent_news_rows_for_mode(settings: Settings, *, broadtape: bool, limit: int = 12) -> list[dict[str, Any]]:
    db = Database(settings.database_path)
    comparator = "LIKE" if broadtape else "NOT LIKE"
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT id, provider, primary_symbol, headline, published_at, received_at, provider_published_at
            FROM news_articles
            WHERE primary_symbol {comparator} 'BROADTAPE:%'
            ORDER BY COALESCE(provider_published_at, published_at, received_at) DESC, id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def _news_mode_counts(settings: Settings) -> dict[str, int]:
    db = Database(settings.database_path)
    with db.connect() as conn:
        broadtape = int(
            conn.execute(
                "SELECT COUNT(*) FROM news_articles WHERE primary_symbol LIKE 'BROADTAPE:%'"
            ).fetchone()[0]
        )
        ticker_specific = int(
            conn.execute(
                "SELECT COUNT(*) FROM news_articles WHERE primary_symbol NOT LIKE 'BROADTAPE:%'"
            ).fetchone()[0]
        )
    return {"broadtape": broadtape, "ticker_specific": ticker_specific}


def _render_broadtape_comparison_markdown(payload: dict[str, Any]) -> str:
    probe = payload.get("probe") or {}
    comparison = payload.get("comparison") or {}
    lines = [
        "# IBKR BroadTape vs ticker-specific news probe",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Status: {probe.get('interpretation')}",
        f"- Requested providers: {', '.join(probe.get('requested_provider_codes') or []) or 'none'}",
        f"- Returned providers: {', '.join(sorted((probe.get('returned_providers') or {}).keys())) or 'none'}",
        f"- Subscribed BroadTape providers: {', '.join(probe.get('subscribed_provider_codes') or []) or 'none'}",
        f"- Unsupported BroadTape providers: {', '.join(probe.get('unsupported_provider_codes') or []) or 'none'}",
        f"- BroadTape ticks observed: {probe.get('tick_count', 0)}",
        f"- BroadTape rows created: {probe.get('rows_created', 0)} / duplicates: {probe.get('duplicates', 0)}",
        "",
        "## Count delta",
        "",
        "| Mode | Before | After | Delta |",
        "|---|---:|---:|---:|",
    ]
    before = comparison.get("before_counts") or {}
    after = comparison.get("after_counts") or {}
    for key, label in (("broadtape", "BroadTape"), ("ticker_specific", "Ticker-specific")):
        b = int(before.get(key) or 0)
        a = int(after.get(key) or 0)
        lines.append(f"| {label} | {b} | {a} | {a - b} |")
    lines.extend(["", "## Probe headlines", ""])
    headlines = probe.get("headlines") or []
    if not headlines:
        lines.append("- No BroadTape headlines observed during this probe window.")
    for item in headlines[:10]:
        lines.append(
            f"- {item.get('published_at') or item.get('received_at')} "
            f"[{item.get('provider')}] {item.get('headline')}"
        )
    lines.extend(["", "## Recent BroadTape rows in DB", ""])
    broad_rows = comparison.get("recent_broadtape_rows") or []
    if not broad_rows:
        lines.append("- none")
    for item in broad_rows[:10]:
        lines.append(
            f"- #{item.get('id')} {item.get('provider_published_at') or item.get('published_at') or item.get('received_at')} "
            f"[{item.get('provider')}/{item.get('primary_symbol')}] {item.get('headline')}"
        )
    lines.extend(["", "## Recent ticker-specific rows in DB", ""])
    ticker_rows = comparison.get("recent_ticker_specific_rows") or []
    if not ticker_rows:
        lines.append("- none")
    for item in ticker_rows[:10]:
        lines.append(
            f"- #{item.get('id')} {item.get('provider_published_at') or item.get('published_at') or item.get('received_at')} "
            f"[{item.get('provider')}/{item.get('primary_symbol')}] {item.get('headline')}"
        )
    errors = probe.get("errors") or []
    if errors:
        lines.extend(["", "## Errors / warnings", ""])
        for error in errors:
            lines.append(f"- {error}")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Read-only market-data/news subscriptions only.",
            "- No broker order submitted.",
            "- Transmit remains disabled/not used.",
        ]
    )
    return "\n".join(lines) + "\n"


def command_ibkr_news_broadtape_probe(
    settings: Settings,
    providers: str | None,
    seconds: float,
    save_to_db: bool,
    ibkr_client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.ibkr_broadtape_news import IBKRBroadTapeNewsProbe

    provider_codes = (
        tuple(item.strip().upper() for item in providers.split(",") if item.strip())
        if providers
        else settings.ibkr_broadtape_provider_codes
    )
    before_counts = _news_mode_counts(settings)
    probe = IBKRBroadTapeNewsProbe(
        settings,
        provider_codes=provider_codes,
        seconds=seconds,
        save_to_db=save_to_db,
        client_id=ibkr_client_id,
    )
    result = probe.run_probe().as_dict()
    after_counts = _news_mode_counts(settings)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "command": "ibkr-news-broadtape-probe",
        "probe": result,
        "comparison": {
            "before_counts": before_counts,
            "after_counts": after_counts,
            "recent_broadtape_rows": _recent_news_rows_for_mode(settings, broadtape=True),
            "recent_ticker_specific_rows": _recent_news_rows_for_mode(settings, broadtape=False),
        },
    }
    if json_output_path:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown = _render_broadtape_comparison_markdown(payload)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_console_safe_text(markdown))
        if output:
            print(f"BroadTape comparison Markdown written: {output}")
        if json_output_path:
            print(f"BroadTape comparison JSON written: {json_output_path}")
    return payload


def command_news_subscription_mesh(
    settings: Settings,
    profile_json_path: Path | None,
    lookback_minutes: int | None,
    max_temp_symbols: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.news_subscription_mesh import (
        NewsSubscriptionMeshConfig,
        build_news_subscription_mesh,
        render_news_subscription_mesh_markdown,
        write_news_subscription_mesh_json,
        write_news_subscription_mesh_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)

    universe_json_path = (
        _coalesce_profile_path(None, profile, "paths", "universe_json", profile_base)
        or Path("config/news_subscription_mesh_universe_v1.json")
    )
    database_path = (
        _coalesce_profile_path(None, profile, "paths", "database_path", profile_base)
        or settings.database_path
    )
    output = (
        _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
        or Path("exports/m235_news_subscription_mesh.md")
    )
    json_output_path = (
        _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
        or Path("exports/m235_news_subscription_mesh.json")
    )

    config = NewsSubscriptionMeshConfig(
        database_path=database_path,
        universe_json_path=universe_json_path,
        lookback_minutes=int(_coalesce_profile_value(lookback_minutes, profile, "mesh", "lookback_minutes", 240)),
        temp_ttl_minutes=int(_coalesce_profile_value(None, profile, "mesh", "temp_ttl_minutes", 180)),
        max_temp_symbols=int(_coalesce_profile_value(max_temp_symbols, profile, "mesh", "max_temp_symbols", 40)),
        max_core_live_symbols=int(_coalesce_profile_value(None, profile, "mesh", "max_core_live_symbols", 175)),
        min_temp_confidence=str(_coalesce_profile_value(None, profile, "mesh", "min_temp_confidence", "medium")),
        include_dow_jones_geopolitical_candidates=bool(
            _coalesce_profile_value(None, profile, "mesh", "include_dow_jones_geopolitical_candidates", True)
        ),
        dow_jones_geopolitical_lookback_minutes=int(
            _coalesce_profile_value(None, profile, "mesh", "dow_jones_geopolitical_lookback_minutes", 720)
        ),
    )
    payload = build_news_subscription_mesh(config)
    written = write_news_subscription_mesh_markdown(output, payload) if output else None
    json_written = write_news_subscription_mesh_json(json_output_path, payload) if json_output_path else None
    markdown = render_news_subscription_mesh_markdown(payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_console_safe_text(markdown))
        if written:
            print(f"News subscription mesh Markdown written: {written}")
        if json_written:
            print(f"News subscription mesh JSON written: {json_written}")
    return payload


def command_cross_provider_event_roles(
    settings: Settings,
    profile_json_path: Path | None,
    lookback_minutes: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.cross_provider_event_roles import (
        CrossProviderEventRoleConfig,
        build_cross_provider_event_roles,
        render_cross_provider_event_roles_markdown,
        write_cross_provider_event_roles_json,
        write_cross_provider_event_roles_markdown,
    )

    profile: dict[str, Any] = {}
    profile_base: Path | None = None
    if profile_json_path is not None:
        profile, profile_base = _load_profile_json(profile_json_path)
    database_path = (
        _coalesce_profile_path(None, profile, "paths", "database_path", profile_base)
        or settings.database_path
    )
    mesh_json_path = (
        _coalesce_profile_path(None, profile, "paths", "mesh_json", profile_base)
        or Path("exports/m235_news_subscription_mesh.json")
    )
    outcome_log_jsonl_path = _coalesce_profile_path(None, profile, "outputs", "outcome_log_jsonl_path", profile_base)
    output = (
        _coalesce_profile_path(output, profile, "outputs", "output", profile_base)
        or Path("exports/m236_cross_provider_event_roles.md")
    )
    json_output_path = (
        _coalesce_profile_path(json_output_path, profile, "outputs", "json_output_path", profile_base)
        or Path("exports/m236_cross_provider_event_roles.json")
    )
    config = CrossProviderEventRoleConfig(
        database_path=database_path,
        mesh_json_path=mesh_json_path,
        outcome_log_jsonl_path=outcome_log_jsonl_path,
        lookback_minutes=int(_coalesce_profile_value(lookback_minutes, profile, "classifier", "lookback_minutes", 240)),
        max_news_rows=int(_coalesce_profile_value(None, profile, "classifier", "max_news_rows", 2000)),
        max_external_rows=int(_coalesce_profile_value(None, profile, "classifier", "max_external_rows", 1000)),
        cluster_window_minutes=int(_coalesce_profile_value(None, profile, "classifier", "cluster_window_minutes", 45)),
        write_outcome_log=bool(_coalesce_profile_value(None, profile, "classifier", "write_outcome_log", bool(outcome_log_jsonl_path))),
    )
    payload = build_cross_provider_event_roles(config)
    written = write_cross_provider_event_roles_markdown(output, payload) if output else None
    json_written = write_cross_provider_event_roles_json(json_output_path, payload) if json_output_path else None
    markdown = render_cross_provider_event_roles_markdown(payload)
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(_console_safe_text(markdown))
        if written:
            print(f"Cross-provider event roles Markdown written: {written}")
        if json_written:
            print(f"Cross-provider event roles JSON written: {json_written}")
    return payload


def _render_news_lineage_v2_rebuild_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# M253 news lineage v2 rebuild",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Version: `{payload.get('version')}`",
        f"- Database: `{payload.get('database_path')}`",
        f"- Window: `{payload.get('start') or 'all'}` -> `{payload.get('end') or 'all'}`",
        f"- Synced legacy news rows: {payload.get('synced_legacy_news_articles')}",
        f"- Assigned raw articles: {payload.get('assigned_articles')}",
        f"- Clusters touched: {payload.get('clusters_touched')}",
        f"- Event-cluster role rows: {payload.get('event_cluster_role_count')}",
        f"- Asset candidates: {payload.get('asset_candidate_count')}",
        "",
        "## Cutover",
        "",
        "- Ranking now prefers `news_event_clusters` + `news_event_cluster_roles` + `news_asset_candidates`.",
        "- Outcome backfill now prefers deduplicated `event_cluster_id`/symbol candidates.",
        "- Raw news and provider entities remain preserved; this command derives lineage only.",
        "",
        "## Safety",
        "",
        "- No network/news API fetch.",
        "- No broker order submitted.",
        "- Transmit/live routing/live threshold promotion not used.",
    ]
    failures = payload.get("failed_legacy_news_articles") or []
    if failures:
        lines.extend(["", "## Legacy sync failures", ""])
        for item in failures[:10]:
            lines.append(f"- legacy_news_id={item.get('legacy_news_id')}: {item.get('error')}")
    return "\n".join(lines) + "\n"


def command_news_lineage_v2_rebuild(
    settings: Settings,
    start: str | None,
    end: str | None,
    limit: int | None,
    bucket_minutes: int,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.news_lineage_v2 import rebuild_news_lineage_v2

    payload = rebuild_news_lineage_v2(
        str(settings.database_path),
        start=start,
        end=end,
        limit=limit,
        bucket_minutes=bucket_minutes,
    )
    if json_output_path:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown = _render_news_lineage_v2_rebuild_markdown(payload)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_console_safe_text(markdown))
        if output:
            print(f"M253 lineage rebuild Markdown written: {output}")
        if json_output_path:
            print(f"M253 lineage rebuild JSON written: {json_output_path}")
    return payload


def _split_csv_text(value: str | None, *, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if not value:
        return default
    return tuple(part.strip() for part in str(value).split(",") if part.strip())


def _split_csv_ints(value: str | None, *, default: tuple[int, ...] = ()) -> tuple[int, ...]:
    items: list[int] = []
    for part in _split_csv_text(value):
        try:
            items.append(int(part))
        except ValueError:
            continue
    return tuple(items) or default


def command_reaction_label_foundation_build(
    settings: Settings,
    start: str | None,
    end: str | None,
    limit: int | None,
    windows: str | None,
    decision_lags_seconds: str | None,
    include_core: bool,
    include_event_proxies: bool,
    create_label_stubs: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.reaction_research_engine import (
        build_reaction_label_foundation,
        render_reaction_label_foundation_markdown,
    )

    payload = build_reaction_label_foundation(
        settings.database_path,
        start=start,
        end=end,
        limit=limit,
        windows=_split_csv_text(windows, default=("5m", "15m", "1h", "close")),
        decision_lags_seconds=_split_csv_ints(decision_lags_seconds, default=(0, 30, 60, 180)),
        include_core=include_core,
        include_event_proxies=include_event_proxies,
        create_label_stubs=create_label_stubs,
    )
    markdown = render_reaction_label_foundation_markdown(payload)
    if json_output_path:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(_console_safe_text(markdown))
        if output:
            print(f"M253 reaction label foundation Markdown written: {output}")
        if json_output_path:
            print(f"M253 reaction label foundation JSON written: {json_output_path}")
    return payload
