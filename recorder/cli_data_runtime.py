from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from recorder.config import Settings


def _create_ai_service(settings: Settings, **kwargs):
    from recorder.ai_provider import PermanentAnalysisError
    from recorder.analysis_service import AIAnalysisService

    try:
        return AIAnalysisService(settings, **kwargs)
    except PermanentAnalysisError as exc:
        raise SystemExit(f"AI configuration error: {exc}") from exc


def command_attribution_backfill(settings: Settings, limit: int | None) -> None:
    """Append deterministic v0.14 attribution records for stored news events."""
    from recorder.analysis_database import AnalysisDatabase

    result = AnalysisDatabase(settings.database_path).attribution_backfill(limit=limit)
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_attribution_report(settings: Settings, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    result = AnalysisDatabase(settings.database_path).attribution_report(limit=limit)
    print(json.dumps(result, indent=2, ensure_ascii=False))




def command_cluster_report(settings: Settings, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase
    db = AnalysisDatabase(settings.database_path)
    print("Recent event clusters:")
    for row in db.recent_clusters(limit):
        print(
            f"Cluster #{row['id']} members={row['member_count']} "
            f"scope={row['event_scope']} type={row['event_type']} family={row['cluster_family']} subject={row['subject_key']} "
            f"subject_symbol={row['subject_symbol'] or '-'} "
            f"target={row['target_symbol'] or row['primary_symbol']} "
            f"anchor={row['event_anchor']} source={row['source_tier']}\n"
            f"  {row['headline'] or ''}"
        )


def command_official_add(
    settings: Settings,
    news_id: int,
    source_type: str,
    source_url: str,
    title: str,
    file_path: Path,
    *,
    reanalyze: bool = True,
) -> None:
    import hashlib
    from dataclasses import replace
    from recorder.analysis_database import AnalysisDatabase

    content = file_path.read_text(encoding="utf-8")
    db = AnalysisDatabase(settings.database_path)
    document_id = db.add_manual_official_document(news_id, source_type, source_url, title, content)
    print(f"Stored official document #{document_id} for news #{news_id}.")

    if not reanalyze:
        return

    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
    revised = replace(
        settings,
        ai_prompt_version=f"{settings.ai_prompt_version}-official-{digest}",
        ai_body_wait_seconds=0,
    )
    db.enqueue_news_job(
        news_id,
        revised.analysis_key,
        revised.ai_provider,
        revised.ai_model,
        revised.ai_prompt_version,
        priority=1000,
    )
    service = _create_ai_service(revised, db=db)
    processed = service.process_batch(1)
    print(
        f"Official-source reanalysis processed={processed}; "
        f"analysis_key={revised.analysis_key}"
    )


def command_official_report(settings: Settings, news_id: int | None) -> None:
    from recorder.analysis_database import AnalysisDatabase
    db = AnalysisDatabase(settings.database_path)
    print(json.dumps(db.official_source_summary(), indent=2, ensure_ascii=False))
    if news_id is not None:
        for row in db.official_documents_for_news(news_id):
            print(f"#{row['id']} {row['status']} {row['source_type']} {row['source_url']} {row['title']}")


def command_sec_sync(
    settings: Settings,
    symbols_value: str | None,
    forms_value: str,
    limit_per_symbol: int,
    force: bool = False,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.sec_edgar import SecEdgarClient, SecEdgarCollector

    symbols = tuple(
        item.strip().upper()
        for item in (symbols_value or ",".join(settings.watch_symbols)).split(",")
        if item.strip()
    )
    forms = tuple(item.strip().upper() for item in forms_value.split(",") if item.strip())
    if not symbols:
        raise SystemExit("Provide at least one ticker with --symbols.")
    if not forms:
        raise SystemExit("Provide at least one SEC form type with --forms.")
    client = SecEdgarClient(
        user_agent=settings.sec_edgar_user_agent,
        timeout_seconds=settings.sec_edgar_timeout_seconds,
        max_bytes=settings.sec_edgar_max_bytes,
        min_interval_seconds=settings.sec_edgar_min_interval_seconds,
    )
    collector = SecEdgarCollector(AnalysisDatabase(settings.database_path), client)
    print(
        json.dumps(
            collector.sync_symbols(
                symbols,
                forms=forms,
                limit_per_symbol=limit_per_symbol,
                force=force,
            ),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_sec_report(settings: Settings, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    print(json.dumps(db.official_source_summary(), indent=2, ensure_ascii=False))
    rows = db.recent_sec_documents(limit)
    if not rows:
        print("\nNo SEC EDGAR documents are stored yet.")
        return
    print("\nRecent SEC EDGAR documents:")
    for row in rows:
        print(
            f"#{row['id']} [{row['status']}] {row['actual_subject_symbol'] or '-'} "
            f"{row['form_type']} {row['event_type'] or '-'} "
            f"{row['materiality_classification']} accepted={row['accepted_at'] or '-'}\n"
            f"  accession={row['accession_number']} url={row['source_url']}"
        )
        if row.get("last_error"):
            print(f"  error={row['last_error']}")


def command_macro_sync(settings: Settings, source: str, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.macro_sources import MacroOfficialClient, MacroOfficialCollector

    client = MacroOfficialClient(
        user_agent=settings.official_source_user_agent,
        timeout_seconds=settings.official_source_timeout_seconds,
        max_bytes=settings.official_source_max_bytes,
        min_interval_seconds=0.2,
    )
    collector = MacroOfficialCollector(AnalysisDatabase(settings.database_path), client)
    print(
        json.dumps(
            collector.sync(source, limit=limit),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_macro_report(settings: Settings, source: str | None, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    source_type = None
    if source:
        normalized = source.strip().lower()
        source_type = "federal_reserve" if normalized in {"fed", "federal_reserve"} else normalized
    db = AnalysisDatabase(settings.database_path)
    print(json.dumps(db.official_source_summary(), indent=2, ensure_ascii=False))
    rows = db.recent_official_documents(limit=limit, source_type=source_type)
    if not rows:
        print("\nNo matching official macro documents are stored yet.")
        return
    print("\nRecent official macro documents:")
    for row in rows:
        print(
            f"#{row['id']} [{row['status']}] {row['source_type']} "
            f"{row['event_type'] or '-'} {row['materiality_classification']} "
            f"available={row['available_at'] or '-'}\n"
            f"  {row['title']}\n"
            f"  {row['source_url']}"
        )


def command_macro_lineage_v1_rebuild(
    settings: Settings,
    source: str,
    start: str | None,
    end: str | None,
    limit: int | None,
    universe: str,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.macro_event_lineage import (
        rebuild_macro_event_lineage_from_observations,
        render_macro_event_lineage_rebuild_markdown,
    )

    payload = rebuild_macro_event_lineage_from_observations(
        settings.database_path,
        source=source,
        start=start,
        end=end,
        limit=limit,
        universe=universe,
    )
    if json_output_path is not None:
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown = render_macro_event_lineage_rebuild_markdown(payload)
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(markdown, encoding="utf-8")
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        print(markdown)


def command_financial_facts_sync(
    settings: Settings,
    symbols_value: str,
    tags_value: str | None,
    limit_per_symbol: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.sec_financials import (
        DEFAULT_FINANCIAL_FACT_TAGS,
        SecCompanyFactsClient,
        SecFinancialFactsCollector,
    )

    symbols = tuple(item.strip().upper() for item in symbols_value.split(",") if item.strip())
    if not symbols:
        raise SystemExit("Provide at least one ticker with --symbols.")
    tags = (
        tuple(item.strip() for item in tags_value.split(",") if item.strip())
        if tags_value
        else DEFAULT_FINANCIAL_FACT_TAGS
    )
    client = SecCompanyFactsClient(
        user_agent=settings.sec_edgar_user_agent,
        timeout_seconds=settings.sec_edgar_timeout_seconds,
        max_bytes=max(settings.sec_edgar_max_bytes, 8_000_000),
        min_interval_seconds=settings.sec_edgar_min_interval_seconds,
    )
    collector = SecFinancialFactsCollector(AnalysisDatabase(settings.database_path), client)
    print(
        json.dumps(
            collector.sync_symbols(symbols, tags=tags, limit_per_symbol=limit_per_symbol),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_financial_facts_report(
    settings: Settings,
    symbol: str | None,
    tag: str | None,
    limit: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase

    rows = AnalysisDatabase(settings.database_path).recent_financial_statement_facts(
        limit=limit,
        symbol=symbol,
        tag=tag,
    )
    print(json.dumps({"financial_statement_facts": len(rows)}, indent=2, ensure_ascii=False))
    for row in rows:
        print(
            f"#{row['id']} {row['symbol']} {row['tag']} {row['fiscal_year'] or '-'}{row['fiscal_period'] or ''} "
            f"{row['form']} end={row['end_date']} filed={row['filed_at']} "
            f"value={row['value']} {row['unit']}\n"
            f"  accession={row['accession_number']}"
        )


def command_fred_sync(
    settings: Settings,
    series_value: str,
    start: str | None,
    end: str | None,
    limit_per_series: int,
    timeout: float,
    api_key: str | None,
    build_macro_lineage: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.fred_sources import FredCollector, FredCsvClient
    from recorder.macro_event_lineage import rebuild_macro_event_lineage_from_observations

    series_ids = tuple(item.strip().upper() for item in series_value.split(",") if item.strip())
    if not series_ids:
        raise SystemExit("Provide at least one FRED series with --series.")
    collector = FredCollector(
        AnalysisDatabase(settings.database_path),
        FredCsvClient(timeout_seconds=timeout, retries=2, api_key=api_key or settings.fred_api_key),
    )
    result = collector.sync(
        series_ids,
        start=start,
        end=end,
        limit_per_series=limit_per_series,
    )
    if build_macro_lineage:
        result["macro_lineage"] = rebuild_macro_event_lineage_from_observations(
            settings.database_path,
            source="fred",
            start=start,
            end=end,
            universe="global_core",
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_fred_report(settings: Settings, series_id: str | None, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    rows = AnalysisDatabase(settings.database_path).recent_fred_observations(
        limit=limit,
        series_id=series_id,
    )
    print(json.dumps({"fred_observations": len(rows)}, indent=2, ensure_ascii=False))
    for row in rows:
        print(f"#{row['id']} {row['series_id']} {row['observation_date']} value={row['value']}")


def command_fred_universe_sync(
    settings: Settings,
    universe: str,
    start: str | None,
    end: str | None,
    limit_per_series: int,
    timeout: float,
    api_key: str | None,
    build_macro_lineage: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.fred_sources import FredCollector, FredCsvClient
    from recorder.macro_event_lineage import rebuild_macro_event_lineage_from_observations
    from recorder.macro_universe import fred_series_ids_for_universe, metadata_by_series

    series_ids = fred_series_ids_for_universe(universe)
    metadata = metadata_by_series(universe)
    collector = FredCollector(
        AnalysisDatabase(settings.database_path),
        FredCsvClient(timeout_seconds=timeout, retries=2, api_key=api_key or settings.fred_api_key),
    )
    result = collector.sync(
        series_ids,
        start=start,
        end=end,
        limit_per_series=limit_per_series,
    )
    result["universe"] = universe
    result["series_count"] = len(series_ids)
    result["coverage_metadata"] = [
        {
            "series_id": series_id,
            "label": metadata[series_id].label,
            "region": metadata[series_id].region,
            "category": metadata[series_id].category,
            "source_family": metadata[series_id].source_family,
            "importance": metadata[series_id].importance,
            "freshness_note": metadata[series_id].freshness_note,
        }
        for series_id in series_ids
    ]
    if build_macro_lineage:
        result["macro_lineage"] = rebuild_macro_event_lineage_from_observations(
            settings.database_path,
            source="fred",
            start=start,
            end=end,
            universe=universe,
        )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_fred_universe_report(
    settings: Settings,
    universe: str,
    start: str | None,
    end: str | None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.macro_universe import fred_series_ids_for_universe, metadata_by_series

    series_ids = fred_series_ids_for_universe(universe)
    metadata = metadata_by_series(universe)
    start_value = start or "0001-01-01"
    end_value = end or "9999-12-31"
    rows: list[dict[str, object]] = []
    with AnalysisDatabase(settings.database_path).connect() as conn:
        for series_id in series_ids:
            summary = conn.execute(
                """
                SELECT COUNT(*) count,MIN(observation_date) min_observation_date,MAX(observation_date) max_observation_date
                FROM fred_observations
                WHERE series_id=? AND observation_date>=? AND observation_date<=?
                """,
                (series_id, start_value, end_value),
            ).fetchone()
            latest = conn.execute(
                """
                SELECT observation_date,value,realtime_start,realtime_end,ingestion_version
                FROM fred_observations
                WHERE series_id=? AND observation_date>=? AND observation_date<=?
                ORDER BY observation_date DESC,id DESC
                LIMIT 1
                """,
                (series_id, start_value, end_value),
            ).fetchone()
            item = metadata[series_id]
            count = int(summary["count"] or 0)
            rows.append(
                {
                    "series_id": series_id,
                    "label": item.label,
                    "region": item.region,
                    "category": item.category,
                    "source_family": item.source_family,
                    "importance": item.importance,
                    "freshness_note": item.freshness_note,
                    "observation_count": count,
                    "min_observation_date": str(summary["min_observation_date"] or ""),
                    "max_observation_date": str(summary["max_observation_date"] or ""),
                    "latest_value": None if latest is None else latest["value"],
                    "latest_realtime_start": "" if latest is None else str(latest["realtime_start"] or ""),
                    "latest_realtime_end": "" if latest is None else str(latest["realtime_end"] or ""),
                    "ingestion_version": "" if latest is None else str(latest["ingestion_version"] or ""),
                    "coverage_status": "covered" if count else "no_observations_in_window",
                }
            )
    by_region: dict[str, int] = {}
    by_category: dict[str, int] = {}
    for row in rows:
        if int(row["observation_count"]):
            by_region[str(row["region"])] = by_region.get(str(row["region"]), 0) + 1
            by_category[str(row["category"])] = by_category.get(str(row["category"]), 0) + 1
    print(
        json.dumps(
            {
                "universe": universe,
                "start": start_value,
                "end": end_value,
                "series_count": len(rows),
                "covered_series_count": sum(1 for row in rows if int(row["observation_count"])),
                "missing_series_count": sum(1 for row in rows if not int(row["observation_count"])),
                "covered_by_region": by_region,
                "covered_by_category": by_category,
                "series": rows,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def command_economic_indicator_desk(
    settings: Settings,
    universe: str,
    cutoff_date: str | None,
    lookback_observations: int,
    output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.economic_indicator_desk import (
        build_economic_indicator_desk,
        economic_indicator_desk_markdown,
        write_economic_indicator_desk_report,
    )

    desk = build_economic_indicator_desk(
        AnalysisDatabase(settings.database_path),
        cutoff_date=cutoff_date,
        universe=universe,
        lookback_observations=lookback_observations,
    )
    if output is not None:
        written = write_economic_indicator_desk_report(output, desk)
        print(f"Economic Indicator Specialist Desk report written: {written}")
        return
    if json_output:
        print(json.dumps(desk, indent=2, ensure_ascii=False))
    else:
        print(economic_indicator_desk_markdown(desk))


def command_international_macro_sync(
    settings: Settings,
    source: str,
    start: str | None,
    end: str | None,
    series_value: str | None,
    timeout: float,
    build_macro_lineage: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.international_macro_sources import (
        BokEcosClient,
        BokEcosCollector,
        EstatJapanClient,
        EstatJapanCollector,
        EurostatClient,
        EurostatCollector,
        KosisClient,
        KosisCollector,
    )
    from recorder.macro_event_lineage import rebuild_macro_event_lineage_from_observations

    series_ids = tuple(item.strip().upper() for item in (series_value or "").split(",") if item.strip()) or None
    db = AnalysisDatabase(settings.database_path)
    results: list[dict[str, object]] = []
    sources = ("eurostat", "kosis", "bok_ecos", "estat_japan") if source == "all" else (source,)
    for item in sources:
        if item == "eurostat":
            collector = EurostatCollector(db, EurostatClient(timeout_seconds=timeout, retries=2))
        elif item == "kosis":
            collector = KosisCollector(db, KosisClient(api_key=settings.kosis_api_key, timeout_seconds=timeout, retries=2))
        elif item == "bok_ecos":
            collector = BokEcosCollector(db, BokEcosClient(api_key=settings.bok_ecos_api_key, timeout_seconds=timeout, retries=2))
        elif item == "estat_japan":
            collector = EstatJapanCollector(db, EstatJapanClient(app_id=settings.estat_app_id, timeout_seconds=timeout, retries=2))
        else:
            raise SystemExit(f"Unsupported international macro source: {item}")
        results.append(collector.sync(start=start, end=end, series_ids=series_ids))
    macro_lineage = (
        rebuild_macro_event_lineage_from_observations(
            settings.database_path,
            source="economic_indicators",
            start=start,
            end=end,
            universe="global_core",
        )
        if build_macro_lineage
        else None
    )
    if len(results) == 1:
        payload = dict(results[0])
        if macro_lineage is not None:
            payload["macro_lineage"] = macro_lineage
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        payload = {
            "source": source,
            "start": start,
            "end": end,
            "results": results,
            "stored": sum(int(result.get("stored") or 0) for result in results),
            "updated": sum(int(result.get("updated") or 0) for result in results),
            "skipped": sum(int(result.get("skipped") or 0) for result in results),
            "errors": sum(int(result.get("errors") or 0) for result in results),
        }
        if macro_lineage is not None:
            payload["macro_lineage"] = macro_lineage
        print(json.dumps(payload, indent=2, ensure_ascii=False))


def command_international_macro_report(
    settings: Settings,
    provider: str | None,
    series_id: str | None,
    limit: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase

    rows = AnalysisDatabase(settings.database_path).recent_economic_indicator_observations(
        limit=limit,
        canonical_series_id=series_id,
        provider=provider,
    )
    print(json.dumps({"economic_indicator_observations": len(rows)}, indent=2, ensure_ascii=False))
    for row in rows:
        print(
            f"#{row['id']} {row['canonical_series_id']} {row['provider']} "
            f"{row['observation_date']} value={row['value']} unit={row['unit']}"
        )


def command_technical_indicators_build(
    settings: Settings,
    symbols_value: str,
    start_value: str,
    end_value: str,
    price_source: str,
    lookback_days: int,
    limit_per_symbol: int,
    ibkr_client_id: int | None,
    ibkr_timeout: float,
    ibkr_what_to_show: str,
) -> None:
    from datetime import datetime

    from recorder.analysis_database import AnalysisDatabase
    from recorder.market_outcomes import (
        IBKRHistoricalDailyPriceSource,
        StooqDailyPriceSource,
        YahooChartDailyPriceSource,
    )
    from recorder.technical_indicators import TechnicalIndicatorBuilder

    symbols = tuple(item.strip().upper() for item in symbols_value.split(",") if item.strip())
    if not symbols:
        raise SystemExit("Provide at least one ticker with --symbols.")
    start = datetime.strptime(start_value, "%Y-%m-%d").date()
    end = datetime.strptime(end_value, "%Y-%m-%d").date()
    normalized_source = price_source.strip().lower()
    if normalized_source == "yahoo":
        source = YahooChartDailyPriceSource()
    elif normalized_source == "stooq":
        source = StooqDailyPriceSource()
    elif normalized_source == "ibkr":
        source = IBKRHistoricalDailyPriceSource(
            host=settings.host,
            port=settings.port,
            client_id=ibkr_client_id if ibkr_client_id is not None else settings.client_id + 3000,
            timeout_seconds=ibkr_timeout,
            what_to_show=ibkr_what_to_show,
            use_rth=True,
        )
    else:
        raise SystemExit(f"Unsupported technical indicator price source: {price_source}")
    builder = TechnicalIndicatorBuilder(AnalysisDatabase(settings.database_path), source)
    print(
        json.dumps(
            builder.build(
                symbols=symbols,
                start=start,
                end=end,
                lookback_days=lookback_days,
                limit_per_symbol=limit_per_symbol,
            ),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_technical_indicators_report(
    settings: Settings,
    symbol: str | None,
    price_source: str | None,
    limit: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase

    rows = AnalysisDatabase(settings.database_path).recent_technical_indicators(
        limit=limit,
        symbol=symbol,
        price_source=price_source,
    )
    print(json.dumps({"technical_indicators": len(rows)}, indent=2, ensure_ascii=False))
    for row in rows:
        print(
            f"#{row['id']} {row['symbol']} {row['as_of_date']} source={row['price_source']} "
            f"close={row['close']:.4f} rsi14={row['rsi_14'] if row['rsi_14'] is not None else '-'} "
            f"macd={row['macd'] if row['macd'] is not None else '-'} "
            f"sma20={row['sma_20'] if row['sma_20'] is not None else '-'}"
        )


def command_external_market_sync(
    settings: Settings,
    source: str,
    limit: int,
    alpha_tickers: str,
    marketaux_symbols: str | None,
    marketaux_latest: bool,
    marketaux_search: str | None,
    marketaux_no_filter_entities: bool,
    alpha_topics: str | None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import ExternalMarketSignalCollector
    from recorder.news_subscription_mesh import marketaux_expanded_symbols_from_mesh

    db = AnalysisDatabase(settings.database_path)
    collector = ExternalMarketSignalCollector(db, timeout_seconds=settings.external_source_timeout_seconds)
    result: dict[str, object] = {}
    if source in {"all", "marketaux"}:
        if settings.marketaux_api_key:
            if marketaux_latest:
                mesh_path = Path("exports/m235_news_subscription_mesh.json")
                expanded_symbols: list[str] = []
                if mesh_path.exists():
                    try:
                        mesh_payload = json.loads(mesh_path.read_text(encoding="utf-8"))
                    except Exception:
                        mesh_payload = {}
                    expanded_symbols = marketaux_expanded_symbols_from_mesh(mesh_payload, max_symbols=100)
                marketaux_symbols = ",".join(expanded_symbols) if expanded_symbols else None
                marketaux_search = None
                marketaux_no_filter_entities = False if marketaux_symbols else True
            elif marketaux_search is None:
                marketaux_search = "geopolitical OR sanctions OR Taiwan OR oil OR war"
            result["marketaux"] = collector.collect_marketaux(
                api_key=settings.marketaux_api_key,
                limit=limit,
                symbols=marketaux_symbols if marketaux_latest else (marketaux_symbols or ",".join(settings.geopolitical_news_symbols)),
                search=marketaux_search,
                filter_entities=not marketaux_no_filter_entities,
            )
        else:
            result["marketaux"] = {"requested": 0, "created": 0, "updated": 0, "errors": ["MARKETAUX_API_KEY is not configured"]}
    if source == "alpha_vantage":
        result["alpha_vantage"] = {
            "requested": 0,
            "created": 0,
            "updated": 0,
            "disabled": True,
            "errors": ["Alpha Vantage news sentiment is removed from the active news pipeline; use Marketaux for external crosscheck."],
        }
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_external_market_backfill(
    settings: Settings,
    source: str,
    start: str,
    end: str,
    window_days: int,
    limit_per_window: int,
    max_windows: int | None,
    pause_seconds: float,
    alpha_tickers: str,
    marketaux_symbols: str | None,
    marketaux_mode: str = "query",
    marketaux_query_name: str | None = None,
    marketaux_entity_types: str = "equity,index,etf",
    marketaux_countries: str = "us",
    marketaux_group_similar: bool = True,
    marketaux_must_have_entities: bool = True,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import ExternalMarketSignalCollector, parse_date

    db = AnalysisDatabase(settings.database_path)
    collector = ExternalMarketSignalCollector(db, timeout_seconds=settings.external_source_timeout_seconds)
    sources = ("marketaux",) if source == "all" else (source,)
    results: dict[str, object] = {}
    for item in sources:
        if item == "marketaux":
            results[item] = collector.backfill(
                source=item,
                start=parse_date(start),
                end=parse_date(end),
                window_days=window_days,
                limit_per_window=limit_per_window,
                api_key=settings.marketaux_api_key,
                symbols=marketaux_symbols,
                max_windows=max_windows,
                pause_seconds=pause_seconds,
                marketaux_mode=marketaux_mode,
                marketaux_query_name=marketaux_query_name,
                marketaux_entity_types=marketaux_entity_types,
                marketaux_countries=marketaux_countries,
                marketaux_group_similar=marketaux_group_similar,
                marketaux_must_have_entities=marketaux_must_have_entities,
            )
        elif item == "alpha_vantage":
            results[item] = {
                "requested": 0,
                "created": 0,
                "updated": 0,
                "disabled": True,
                "errors": ["Alpha Vantage news sentiment is removed from the active news pipeline; use Marketaux for external crosscheck."],
            }
    print(json.dumps(results, indent=2, ensure_ascii=False))
    logical_errors: list[str] = []
    for payload in results.values():
        if isinstance(payload, dict):
            logical_errors.extend(str(item) for item in (payload.get("errors") or []) if item)
    if logical_errors:
        raise SystemExit(2)


def command_external_market_coverage(settings: Settings, start: str, end: str) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    start_value = start[:10]
    end_value = end[:10]
    with db.connect() as conn:
        overall = dict(
            conn.execute(
                """
                SELECT COUNT(*) count,MIN(published_at) min_published_at,MAX(published_at) max_published_at
                FROM external_market_signals
                WHERE published_at!='' AND substr(published_at,1,10)>=? AND substr(published_at,1,10)<=?
                """,
                (start_value, end_value),
            ).fetchone()
        )
        by_source = [
            dict(row)
            for row in conn.execute(
                """
                SELECT source,COUNT(*) count,MIN(published_at) min_published_at,MAX(published_at) max_published_at
                FROM external_market_signals
                WHERE published_at!='' AND substr(published_at,1,10)>=? AND substr(published_at,1,10)<=?
                GROUP BY source
                ORDER BY source
                """,
                (start_value, end_value),
            ).fetchall()
        ]
        by_month = [
            dict(row)
            for row in conn.execute(
                """
                SELECT substr(published_at,1,7) month,COUNT(*) count
                FROM external_market_signals
                WHERE published_at!='' AND substr(published_at,1,10)>=? AND substr(published_at,1,10)<=?
                GROUP BY substr(published_at,1,7)
                ORDER BY month
                """,
                (start_value, end_value),
            ).fetchall()
        ]
        by_region_theme = [
            dict(row)
            for row in conn.execute(
                """
                SELECT region,theme,COUNT(*) count,MIN(published_at) min_published_at,MAX(published_at) max_published_at
                FROM external_market_signals
                WHERE published_at!='' AND substr(published_at,1,10)>=? AND substr(published_at,1,10)<=?
                GROUP BY region,theme
                ORDER BY count DESC,region,theme
                LIMIT 50
                """,
                (start_value, end_value),
            ).fetchall()
        ]
    print(
        json.dumps(
            {
                "start": start_value,
                "end": end_value,
                "overall": overall,
                "by_source": by_source,
                "by_month": by_month,
                "by_region_theme": by_region_theme,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def command_standing_regime_build(
    settings: Settings,
    start: str,
    end: str,
    max_gap_days: int,
    active_gap_days: int,
    easing_gap_days: int,
    limit: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.standing_regimes import build_and_store_standing_regimes

    db = AnalysisDatabase(settings.database_path)
    result = build_and_store_standing_regimes(
        db,
        start=start,
        end=end,
        max_gap_days=max_gap_days,
        active_gap_days=active_gap_days,
        easing_gap_days=easing_gap_days,
        limit=limit,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_standing_regime_report(
    settings: Settings,
    cutoff_at: str | None,
    symbol: str | None,
    status: str | None,
    limit: int,
    output: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.standing_regimes import standing_regime_markdown, standing_regime_rows, write_standing_regime_markdown

    db = AnalysisDatabase(settings.database_path)
    rows = standing_regime_rows(
        db,
        cutoff_at=cutoff_at,
        symbol=symbol,
        status=status,
        limit=limit,
    )
    filters = {"cutoff_at": cutoff_at, "symbol": symbol, "status": status, "limit": limit}
    if json_output:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        report = standing_regime_markdown(rows, filters=filters)
        print(report)
    if output is not None:
        path = write_standing_regime_markdown(output, rows, filters=filters)
        print(f"Wrote {path}")


def command_standing_regime_discover_blind(
    settings: Settings,
    start: str,
    end: str,
    include_known: bool,
    min_evidence: int,
    min_duration_days: int,
    min_active_months: int,
    max_gap_days: int,
    exclude_themes: str | None,
    limit: int,
    top_n: int,
    output: Path | None,
    json_output: bool,
    ai_review: bool,
    openai_model: str | None,
    reasoning_effort: str,
    max_ai_candidates: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.standing_regimes import (
        OpenAIBlindRegimeReviewer,
        blind_regime_discovery_markdown,
        discover_blind_regime_candidates,
        write_blind_regime_discovery_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    result = discover_blind_regime_candidates(
        db,
        start=start,
        end=end,
        exclude_known=not include_known,
        min_evidence=min_evidence,
        min_duration_days=min_duration_days,
        min_active_months=min_active_months,
        max_gap_days=max_gap_days,
        exclude_themes=tuple(item.strip() for item in (exclude_themes or "").split(",") if item.strip()),
        limit=limit,
        top_n=top_n,
    )
    if ai_review:
        reviewer = OpenAIBlindRegimeReviewer(
            api_key=settings.openai_api_key or "",
            model=openai_model or settings.ai_model,
            timeout_seconds=settings.ai_api_timeout_seconds,
            reasoning_effort=reasoning_effort,
        )
        result = reviewer.review(result, max_candidates=max_ai_candidates)
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(blind_regime_discovery_markdown(result))
    if output is not None:
        path = write_blind_regime_discovery_markdown(output, result)
        print(f"Wrote {path}")


def command_standing_regime_outcomes(
    settings: Settings,
    symbols: str,
    horizons_minutes: str,
    max_outcome_lag_minutes: float,
    evaluation_limit: int,
    record_current_snapshot: bool,
    evaluate_pending: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.standing_regimes import (
        build_standing_regime_outcome_report,
        standing_regime_outcome_markdown,
        write_standing_regime_outcome_json,
        write_standing_regime_outcome_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    result = build_standing_regime_outcome_report(
        db,
        symbols=symbols,
        horizons_minutes=horizons_minutes,
        record_current_snapshot=record_current_snapshot,
        evaluate_pending=evaluate_pending,
        max_outcome_lag_minutes=max_outcome_lag_minutes,
        evaluation_limit=evaluation_limit,
    )
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(standing_regime_outcome_markdown(result))
    if json_output_path is not None:
        path = write_standing_regime_outcome_json(json_output_path, result)
        print(f"Wrote {path}")
    if output is not None:
        path = write_standing_regime_outcome_markdown(output, result)
        print(f"Wrote {path}")


def command_geopolitical_intelligence(
    settings: Settings,
    lookback_hours: float,
    horizons_minutes: str,
    signal_limit: int,
    max_assets_per_signal: int,
    max_outcome_lag_minutes: float,
    evaluation_limit: int,
    record_current: bool,
    evaluate_pending: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.geopolitical_intelligence import (
        build_geopolitical_intelligence_report,
        geopolitical_intelligence_markdown,
        write_geopolitical_intelligence_json,
        write_geopolitical_intelligence_markdown,
    )

    db = AnalysisDatabase(settings.database_path)
    result = build_geopolitical_intelligence_report(
        db,
        lookback_hours=lookback_hours,
        horizons_minutes=horizons_minutes,
        signal_limit=signal_limit,
        max_assets_per_signal=max_assets_per_signal,
        record_current=record_current,
        evaluate_pending=evaluate_pending,
        max_outcome_lag_minutes=max_outcome_lag_minutes,
        evaluation_limit=evaluation_limit,
    )
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        print(geopolitical_intelligence_markdown(result))
    if json_output_path is not None:
        path = write_geopolitical_intelligence_json(json_output_path, result)
        print(f"Wrote {path}")
    if output is not None:
        path = write_geopolitical_intelligence_markdown(output, result)
        print(f"Wrote {path}")


def command_benzinga_external_signals_build(
    settings: Settings,
    start: str,
    end: str,
    symbols_value: str | None,
    limit: int,
    include_non_geopolitical: bool,
    dynamic_total_limit: int = 0,
    dynamic_per_symbol_limit: int = 0,
    dynamic_priority_per_symbol_limit: int = 0,
    dynamic_priority_symbols_value: str | None = None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import ExternalMarketSignalCollector, parse_date

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    dynamic_priority_symbols = tuple(
        item.strip().upper() for item in (dynamic_priority_symbols_value or "").split(",") if item.strip()
    )
    db = AnalysisDatabase(settings.database_path)
    collector = ExternalMarketSignalCollector(db, timeout_seconds=settings.external_source_timeout_seconds)
    result = collector.convert_benzinga_news(
        start=parse_date(start),
        end=parse_date(end),
        symbols=symbols or None,
        limit=limit,
        keywords_only=not include_non_geopolitical,
        dynamic_total_limit=dynamic_total_limit,
        dynamic_per_symbol_limit=dynamic_per_symbol_limit,
        dynamic_priority_per_symbol_limit=dynamic_priority_per_symbol_limit,
        dynamic_priority_symbols=dynamic_priority_symbols or None,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_dow_jones_external_signals_build(
    settings: Settings,
    start: str,
    end: str,
    symbols_value: str | None,
    limit: int,
    include_non_geopolitical: bool,
    dynamic_total_limit: int = 0,
    dynamic_per_symbol_limit: int = 0,
    dynamic_priority_per_symbol_limit: int = 0,
    dynamic_priority_symbols_value: str | None = None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import ExternalMarketSignalCollector, parse_date

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    dynamic_priority_symbols = tuple(
        item.strip().upper() for item in (dynamic_priority_symbols_value or "").split(",") if item.strip()
    )
    db = AnalysisDatabase(settings.database_path)
    collector = ExternalMarketSignalCollector(db, timeout_seconds=settings.external_source_timeout_seconds)
    result = collector.convert_dow_jones_news(
        start=parse_date(start),
        end=parse_date(end),
        symbols=symbols or None,
        limit=limit,
        keywords_only=not include_non_geopolitical,
        dynamic_total_limit=dynamic_total_limit,
        dynamic_per_symbol_limit=dynamic_per_symbol_limit,
        dynamic_priority_per_symbol_limit=dynamic_priority_per_symbol_limit,
        dynamic_priority_symbols=dynamic_priority_symbols or None,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_breaking_news_ingest(
    settings: Settings,
    headline: str,
    symbols_value: str | None,
    published_at: str | None,
    source_label: str,
    url: str,
    sentiment_score: float | None,
    confidence: float | None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import ExternalMarketSignalCollector

    symbols = tuple(item.strip().upper() for item in (symbols_value or "").split(",") if item.strip())
    db = AnalysisDatabase(settings.database_path)
    collector = ExternalMarketSignalCollector(db, timeout_seconds=settings.external_source_timeout_seconds)
    result = collector.ingest_breaking_news(
        headline=headline,
        published_at=published_at,
        symbols=symbols,
        source_label=source_label,
        url=url,
        sentiment_score=sentiment_score,
        confidence=confidence,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_emergency_breaking_backfill(
    settings: Settings,
    start: str,
    end: str,
    source: str,
    limit: int,
    min_urgency: str,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import ExternalMarketSignalCollector, parse_date

    db = AnalysisDatabase(settings.database_path)
    collector = ExternalMarketSignalCollector(db, timeout_seconds=settings.external_source_timeout_seconds)
    result = collector.backfill_emergency_breaking_news(
        start=parse_date(start),
        end=parse_date(end),
        source=source,
        limit=limit,
        min_urgency=min_urgency,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


def command_external_market_report(
    settings: Settings,
    source: str | None,
    region: str | None,
    theme: str | None,
    limit: int,
    json_output: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.external_market_sources import format_external_market_signal_report

    rows = AnalysisDatabase(settings.database_path).recent_external_market_signals(
        source=source,
        region=region,
        theme=theme,
        limit=limit,
    )
    if json_output:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print(format_external_market_signal_report(rows))


def command_historical_import(
    settings: Settings,
    event_type: str,
    symbols_value: str,
    start_value: str,
    end_value: str,
    limit_per_symbol: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.historical_importer import HistoricalEventImporter, parse_date
    from recorder.sec_edgar import SecEdgarClient

    symbols = tuple(item.strip().upper() for item in symbols_value.split(",") if item.strip())
    if not symbols:
        raise SystemExit("Provide at least one ticker with --symbols.")
    client = SecEdgarClient(
        user_agent=settings.sec_edgar_user_agent,
        timeout_seconds=settings.sec_edgar_timeout_seconds,
        max_bytes=settings.sec_edgar_max_bytes,
        min_interval_seconds=settings.sec_edgar_min_interval_seconds,
    )
    importer = HistoricalEventImporter(AnalysisDatabase(settings.database_path), client)
    print(
        json.dumps(
            importer.import_sec_earnings_guidance(
                symbols=symbols,
                start=parse_date(start_value),
                end=parse_date(end_value),
                event_type=event_type,
                limit_per_symbol=limit_per_symbol,
            ),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_historical_report(settings: Settings, event_type: str | None, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    rows = db.recent_historical_events(limit=limit, event_type=event_type)
    print(json.dumps({"historical_events": len(rows)}, indent=2, ensure_ascii=False))
    if not rows:
        print("\nNo historical events are stored yet.")
        return
    print("\nRecent historical events:")
    for row in rows:
        try:
            reasons = json.loads(row["replay_eligibility_reasons_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            reasons = []
        reason_text = "" if not reasons else " reasons=" + ",".join(str(item) for item in reasons)
        print(
            f"#{row['id']} {row['event_type']}/{row['event_subtype']} "
            f"{row['actual_subject_symbol']} available={row['available_at']} "
            f"session={row['market_session']} replay={row['replay_eligibility']}{reason_text}\n"
            f"  doc=#{row['official_document_id']} {row['form_type']} {row['accession_number']} "
            f"{row['materiality_classification']}\n"
            f"  {row['title']}\n"
            f"  {row['source_url']}"
        )


def command_replay_corpus_build(
    settings: Settings,
    event_type: str | None,
    start: str | None,
    end: str | None,
    limit: int,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_corpus import ReplayCorpusBuilder

    builder = ReplayCorpusBuilder(AnalysisDatabase(settings.database_path))
    print(
        json.dumps(
            builder.build(start=start, end=end, event_type=event_type, limit=limit),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_replay_corpus_report(settings: Settings, event_type: str | None, limit: int) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    rows = db.recent_replay_corpus(limit=limit, event_type=event_type)
    print(json.dumps({"replay_corpus_rows": len(rows)}, indent=2, ensure_ascii=False))
    if not rows:
        print("\nNo replay corpus rows are stored yet.")
        return
    print("\nRecent replay corpus rows:")
    for row in rows:
        reason = f" blocker={row['blocker_reason']}" if row["blocker_reason"] else ""
        print(
            f"#{row['id']} {row['event_type']}/{row['event_subtype']} "
            f"{row['actual_subject_symbol']} replay={row['replay_timestamp']} "
            f"session={row['session']} leakage={row['leakage_status']} "
            f"eligibility={row['eligibility_status']}{reason}\n"
            f"  event=#{row['historical_event_id']} doc=#{row['official_document_id']} "
            f"snapshot={row['input_snapshot_hash'][:16]}"
        )


def command_replay_outcome_build(
    settings: Settings,
    event_type: str | None,
    start: str | None,
    end: str | None,
    horizons: str,
    benchmark: str,
    limit: int,
    price_source: str,
    ibkr_client_id: int | None,
    ibkr_timeout: float,
    ibkr_what_to_show: str,
    ibkr_all_sessions: bool,
    ibkr_bar_size: str,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.market_outcomes import (
        IBKRHistoricalDailyPriceSource,
        IBKRHistoricalIntradayPriceSource,
        ReplayMarketOutcomeBuilder,
        StooqDailyPriceSource,
        YahooChartDailyPriceSource,
        parse_horizons,
    )

    parsed_horizons = parse_horizons(horizons)
    has_intraday = any(not str(item).upper().endswith("D") for item in parsed_horizons)
    normalized_source = price_source.strip().lower()
    if normalized_source == "yahoo":
        if has_intraday:
            raise SystemExit("Yahoo source supports daily horizons only. Use --price-source ibkr for intraday horizons.")
        source = YahooChartDailyPriceSource()
    elif normalized_source == "stooq":
        if has_intraday:
            raise SystemExit("Stooq source supports daily horizons only. Use --price-source ibkr for intraday horizons.")
        source = StooqDailyPriceSource()
    elif normalized_source == "ibkr":
        if has_intraday:
            source = IBKRHistoricalIntradayPriceSource(
                host=settings.host,
                port=settings.port,
                client_id=ibkr_client_id if ibkr_client_id is not None else settings.client_id + 1000,
                timeout_seconds=ibkr_timeout,
                what_to_show=ibkr_what_to_show,
                bar_size=ibkr_bar_size,
                use_rth=not ibkr_all_sessions,
            )
        else:
            source = IBKRHistoricalDailyPriceSource(
                host=settings.host,
                port=settings.port,
                client_id=ibkr_client_id if ibkr_client_id is not None else settings.client_id + 1000,
                timeout_seconds=ibkr_timeout,
                what_to_show=ibkr_what_to_show,
                use_rth=not ibkr_all_sessions,
            )
    else:
        raise SystemExit(f"Unsupported price source: {price_source}")

    builder = ReplayMarketOutcomeBuilder(AnalysisDatabase(settings.database_path), source)
    print(
        json.dumps(
            builder.build(
                start=start,
                end=end,
                event_type=event_type,
                horizons=parsed_horizons,
                benchmark_symbol=benchmark,
                limit=limit,
            ),
            indent=2,
            ensure_ascii=False,
        )
    )


def command_replay_outcome_report(
    settings: Settings,
    event_type: str | None,
    limit: int,
    price_source: str | None = None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    rows = db.recent_replay_market_outcomes(
        limit=limit,
        event_type=event_type,
        price_source=price_source,
    )
    print(json.dumps({"replay_market_outcomes": len(rows)}, indent=2, ensure_ascii=False))
    if not rows:
        print("\nNo replay market outcomes are stored yet.")
        return
    print("\nRecent replay market outcomes:")
    for row in rows:
        raw = row["raw_return"]
        excess = row["excess_return"]
        raw_text = "-" if raw is None else f"{float(raw) * 100:.2f}%"
        excess_text = "-" if excess is None else f"{float(excess) * 100:.2f}%"
        reason = f" blocker={row['blocker_reason']}" if row["blocker_reason"] else ""
        print(
            f"#{row['id']} {row['event_type']}/{row['event_subtype']} "
            f"{row['symbol']} {row['horizon']} {row['baseline_at']}->{row['reaction_at']} "
            f"return={raw_text} excess={excess_text} direction={row['direction_label']} "
            f"status={row['price_status']}{reason}\n"
            f"  corpus=#{row['replay_corpus_id']} benchmark={row['benchmark_symbol'] or '-'} "
            f"source={row['price_source']}"
        )


def command_replay_outcome_compare(
    settings: Settings,
    event_type: str | None,
    symbol: str | None,
    limit: int,
    price_source: str | None = None,
    json_output: bool = False,
    output: Path | None = None,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.outcome_comparison import ReplayOutcomeComparisonReporter, format_comparison_text

    db = AnalysisDatabase(settings.database_path)
    rows = db.replay_outcome_comparison_rows(
        limit=limit,
        event_type=event_type,
        symbol=symbol,
        price_source=price_source,
    )
    comparison = ReplayOutcomeComparisonReporter(rows).build()
    if json_output:
        payload = json.dumps(comparison.as_dict(), indent=2, ensure_ascii=False)
        if output:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(payload + "\n", encoding="utf-8")
            print(f"JSON comparison written: {output.resolve()}")
            return
        print(payload)
        return
    text = format_comparison_text(comparison)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text + "\n", encoding="utf-8")
        print(f"Replay outcome comparison written: {output.resolve()}")
        return
    print(json.dumps({"replay_corpus_events": len(comparison.events)}, indent=2, ensure_ascii=False))
    print()
    print(text)


def command_replay_dataset_export(
    settings: Settings,
    output: Path | None,
    file_format: str,
    event_type: str | None,
    symbol: str | None,
    price_source: str | None,
    horizon: str | None,
    start: str | None,
    end: str | None,
    limit: int,
    include_blocked: bool,
    include_input_snapshot: bool,
) -> None:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.replay_dataset import ReplayDatasetExporter

    normalized_format = file_format.strip().lower()
    if normalized_format not in {"csv", "jsonl"}:
        raise SystemExit(f"Unsupported dataset format: {file_format}")
    if output is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output = Path("exports") / f"replay_dataset_{stamp}.{normalized_format}"
    db = AnalysisDatabase(settings.database_path)
    filters = {
        "event_type": event_type,
        "symbol": symbol.strip().upper() if symbol else None,
        "price_source": price_source,
        "horizon": horizon.strip().upper() if horizon else None,
        "start": start,
        "end": end,
        "limit": limit,
        "include_blocked": include_blocked,
    }
    rows = db.replay_dataset_export_rows(
        limit=limit,
        event_type=event_type,
        symbol=symbol,
        price_source=price_source,
        horizon=horizon,
        start=start,
        end=end,
        include_blocked=include_blocked,
    )
    result = ReplayDatasetExporter(rows).export(
        output,
        file_format=normalized_format,
        include_input_snapshot=include_input_snapshot,
        include_blocked=include_blocked,
        filters=filters,
    )
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
