from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from recorder.config import Settings


def _console_safe_text(text: str) -> str:
    return text.encode("ascii", errors="replace").decode("ascii")


def command_ibkr_options_chain_scan(
    settings: Settings,
    symbols_value: str,
    reference_prices_value: str | None,
    underlying_sec_types_value: str | None,
    underlying_exchanges_value: str | None,
    min_dte: int,
    max_dte: int,
    strikes_around: int,
    max_expirations: int,
    timeout: float,
    client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ibkr_options import (
        OptionChainScanConfig,
        option_chain_scan_markdown,
        parse_reference_prices,
        parse_symbol_values,
        run_ibkr_option_chain_scan,
        write_option_chain_scan_json,
        write_option_chain_scan_markdown,
    )

    symbols = tuple(item.strip().upper() for item in symbols_value.split(",") if item.strip())
    if not symbols:
        raise ValueError("--symbols must contain at least one ticker")
    config = OptionChainScanConfig(
        host=settings.host,
        port=settings.port,
        client_id=int(client_id if client_id is not None else settings.client_id + 6000),
        symbols=symbols,
        reference_prices=parse_reference_prices(reference_prices_value),
        underlying_sec_types=parse_symbol_values(underlying_sec_types_value),
        underlying_exchanges=parse_symbol_values(underlying_exchanges_value),
        min_dte=min_dte,
        max_dte=max_dte,
        strikes_around=strikes_around,
        max_expirations=max_expirations,
        timeout_seconds=timeout,
    )
    summary = run_ibkr_option_chain_scan(config)
    written = write_option_chain_scan_markdown(output, summary) if output else None
    json_written = write_option_chain_scan_json(json_output_path, summary) if json_output_path else None
    if json_output:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(option_chain_scan_markdown(summary))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_ibkr_options_quote_snapshot(
    settings: Settings,
    symbols_value: str,
    reference_prices_value: str | None,
    underlying_sec_types_value: str | None,
    underlying_exchanges_value: str | None,
    min_dte: int,
    max_dte: int,
    strikes_around: int,
    max_expirations: int,
    max_quote_contracts: int,
    market_data_type: int,
    generic_ticks: str,
    chain_timeout: float,
    quote_seconds: float,
    max_spread_pct: float,
    client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ibkr_options import (
        OptionQuoteSnapshotConfig,
        option_quote_snapshot_markdown,
        parse_reference_prices,
        parse_symbol_values,
        run_ibkr_option_quote_snapshot,
        write_option_quote_snapshot_json,
        write_option_quote_snapshot_markdown,
    )

    symbols = tuple(item.strip().upper() for item in symbols_value.split(",") if item.strip())
    if not symbols:
        raise ValueError("--symbols must contain at least one ticker")
    config = OptionQuoteSnapshotConfig(
        host=settings.host,
        port=settings.port,
        client_id=int(client_id if client_id is not None else settings.client_id + 6100),
        symbols=symbols,
        reference_prices=parse_reference_prices(reference_prices_value),
        underlying_sec_types=parse_symbol_values(underlying_sec_types_value),
        underlying_exchanges=parse_symbol_values(underlying_exchanges_value),
        min_dte=min_dte,
        max_dte=max_dte,
        strikes_around=strikes_around,
        max_expirations=max_expirations,
        max_quote_contracts=max_quote_contracts,
        market_data_type=market_data_type,
        generic_ticks=generic_ticks,
        chain_timeout_seconds=chain_timeout,
        quote_seconds=quote_seconds,
        max_spread_pct=max_spread_pct,
    )
    summary = run_ibkr_option_quote_snapshot(config)
    written = write_option_quote_snapshot_markdown(output, summary) if output else None
    json_written = write_option_quote_snapshot_json(json_output_path, summary) if json_output_path else None
    if json_output:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(option_quote_snapshot_markdown(summary))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_ibkr_account_snapshot(
    settings: Settings,
    account: str | None,
    tags: str,
    timeout_seconds: float,
    include_positions: bool,
    include_open_orders: bool,
    client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ibkr_account import (
        AccountSnapshotConfig,
        account_snapshot_markdown,
        run_ibkr_account_snapshot,
        write_account_snapshot_json,
        write_account_snapshot_markdown,
    )

    config = AccountSnapshotConfig(
        host=settings.host,
        port=settings.port,
        client_id=int(client_id if client_id is not None else settings.client_id + 8000),
        account=account,
        tags=tags,
        timeout_seconds=timeout_seconds,
        include_positions=include_positions,
        include_open_orders=include_open_orders,
    )
    summary = run_ibkr_account_snapshot(config)
    written = write_account_snapshot_markdown(output, summary) if output else None
    json_written = write_account_snapshot_json(json_output_path, summary) if json_output_path else None
    if json_output:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(account_snapshot_markdown(summary))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_ibkr_order_whatif_preview(
    settings: Settings,
    preview_json_path: Path,
    max_orders: int,
    include_human_review: bool,
    allow_fractional: bool,
    timeout_seconds: float,
    client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ibkr_order_preview import (
        OrderWhatIfPreviewConfig,
        order_whatif_preview_markdown,
        run_ibkr_order_whatif_preview,
        write_order_whatif_preview_json,
        write_order_whatif_preview_markdown,
    )

    preview_json_path = preview_json_path.expanduser().resolve()
    preview_payload = json.loads(preview_json_path.read_text(encoding="utf-8"))
    config = OrderWhatIfPreviewConfig(
        host=settings.host,
        port=settings.port,
        client_id=int(client_id if client_id is not None else settings.client_id + 9000),
        timeout_seconds=timeout_seconds,
        max_orders=max_orders,
        include_human_review=include_human_review,
        allow_fractional=allow_fractional,
    )
    summary = run_ibkr_order_whatif_preview(preview_payload, config)
    written = write_order_whatif_preview_markdown(output, summary) if output else None
    json_written = write_order_whatif_preview_json(json_output_path, summary) if json_output_path else None
    if json_output:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(order_whatif_preview_markdown(summary))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_ibkr_short_availability(
    settings: Settings,
    symbols_value: str,
    timeout_seconds: float,
    market_data_type: int,
    client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ibkr_short_availability import (
        ShortAvailabilityConfig,
        run_ibkr_short_availability,
        short_availability_markdown,
        write_short_availability_json,
        write_short_availability_markdown,
    )

    symbols = tuple(item.strip().upper() for item in symbols_value.split(",") if item.strip())
    if not symbols:
        raise ValueError("--symbols must contain at least one ticker")
    summary = run_ibkr_short_availability(
        ShortAvailabilityConfig(
            host=settings.host,
            port=settings.port,
            client_id=int(client_id if client_id is not None else settings.client_id + 9100),
            symbols=symbols,
            timeout_seconds=timeout_seconds,
            market_data_type=market_data_type,
        )
    )
    written = write_short_availability_markdown(output, summary) if output else None
    json_written = write_short_availability_json(json_output_path, summary) if json_output_path else None
    if json_output:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(short_availability_markdown(summary))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_ibkr_stock_mark_snapshot(
    settings: Settings,
    symbols_value: str | None,
    router_json_path: Path | None,
    quote_seconds: float,
    market_data_type: int,
    client_id: int | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> None:
    from recorder.ibkr_stock_marks import (
        StockMarkSnapshotConfig,
        run_ibkr_stock_mark_snapshot,
        stock_mark_snapshot_markdown,
        stock_mark_symbols_from_router_payload,
        write_stock_mark_snapshot_json,
        write_stock_mark_snapshot_markdown,
    )

    symbols: list[str] = []
    seen: set[str] = set()
    if symbols_value:
        for item in symbols_value.split(","):
            symbol = item.strip().upper()
            if symbol and symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    if router_json_path is not None:
        router_json_path = router_json_path.expanduser().resolve()
        router_payload = json.loads(router_json_path.read_text(encoding="utf-8"))
        for symbol in stock_mark_symbols_from_router_payload(router_payload):
            if symbol not in seen:
                seen.add(symbol)
                symbols.append(symbol)
    if not symbols:
        raise ValueError("Provide --symbols or --router-json with at least one paper position symbol")
    summary = run_ibkr_stock_mark_snapshot(
        StockMarkSnapshotConfig(
            host=settings.host,
            port=settings.port,
            client_id=int(client_id if client_id is not None else settings.client_id + 9300),
            symbols=tuple(symbols),
            quote_seconds=quote_seconds,
            market_data_type=market_data_type,
        )
    )
    written = write_stock_mark_snapshot_markdown(output, summary) if output else None
    json_written = write_stock_mark_snapshot_json(json_output_path, summary) if json_output_path else None
    if json_output:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return
    print(stock_mark_snapshot_markdown(summary))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")


def command_options_market_structure_desk(
    quote_json: Path,
    market_data_json: Path | None,
    pin_near_pct: float,
    proximity_near_pct: float,
    max_snapshot_age_minutes: float,
    no_volume_fallback: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.options_market_structure import (
        build_options_market_structure_from_files,
        options_market_structure_markdown,
        write_options_market_structure_json,
        write_options_market_structure_markdown,
    )

    report = build_options_market_structure_from_files(
        quote_json_path=quote_json,
        market_data_json_path=market_data_json,
        fallback_to_volume=not bool(no_volume_fallback),
        pin_near_pct=pin_near_pct,
        proximity_near_pct=proximity_near_pct,
        max_snapshot_age_minutes=max_snapshot_age_minutes,
    )
    written = write_options_market_structure_markdown(output, report) if output else None
    json_written = write_options_market_structure_json(json_output_path, report) if json_output_path else None
    if json_output:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return report
    print(options_market_structure_markdown(report))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return report


def command_options_market_structure_supervisor(
    profile_json: Path | None,
    symbols: str | None,
    max_ticks: int | None,
    interval_seconds: float | None,
    sleep_between_ticks: bool | None,
    execute_actions: bool,
    max_snapshot_age_minutes: float | None,
    run_id: str | None,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.options_market_structure_supervisor import (
        load_options_market_structure_supervisor_config,
        options_market_structure_supervisor_markdown,
        run_options_market_structure_supervisor,
        write_options_market_structure_supervisor_json,
        write_options_market_structure_supervisor_markdown,
    )

    overrides = {
        "symbols": symbols,
        "max_ticks": max_ticks,
        "interval_seconds": interval_seconds,
        "sleep_between_ticks": sleep_between_ticks,
        "execute_actions": execute_actions,
        "max_snapshot_age_minutes": max_snapshot_age_minutes,
    }
    config = load_options_market_structure_supervisor_config(profile_json, overrides=overrides)
    result = run_options_market_structure_supervisor(
        config=config,
        cwd=Path.cwd(),
        run_id=run_id,
        live_json_output_path=json_output_path,
        live_markdown_output_path=output,
    )
    written = write_options_market_structure_supervisor_markdown(output, result) if output else None
    json_written = write_options_market_structure_supervisor_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(options_market_structure_supervisor_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_option_structure_outcome_build(
    settings: Settings,
    options_market_structure_json: Path,
    horizons: str,
    symbols: str | None,
    flat_threshold: float,
    price_source: str,
    ibkr_client_id: int | None,
    ibkr_timeout: float,
    ibkr_what_to_show: str,
    ibkr_bar_size: str,
    ibkr_all_sessions: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.analysis_database import AnalysisDatabase
    from recorder.market_outcomes import IBKRHistoricalIntradayPriceSource
    from recorder.option_structure_outcomes import (
        OptionStructureOutcomeLogger,
        option_structure_outcome_markdown,
        write_option_structure_outcome_json,
        write_option_structure_outcome_markdown,
    )

    report = json.loads(options_market_structure_json.expanduser().read_text(encoding="utf-8"))
    normalized_source = price_source.strip().lower()
    if normalized_source == "none":
        source = None
    elif normalized_source == "ibkr":
        source = IBKRHistoricalIntradayPriceSource(
            host=settings.host,
            port=settings.port,
            client_id=ibkr_client_id if ibkr_client_id is not None else settings.client_id + 1200,
            timeout_seconds=ibkr_timeout,
            what_to_show=ibkr_what_to_show,
            bar_size=ibkr_bar_size,
            use_rth=not ibkr_all_sessions,
        )
    else:
        raise SystemExit(f"Unsupported M222 option-structure outcome price source: {price_source}")

    result = OptionStructureOutcomeLogger(AnalysisDatabase(settings.database_path), source).build(
        options_market_structure=report,
        horizons=horizons,
        flat_threshold=flat_threshold,
        symbols=symbols,
    )
    written = write_option_structure_outcome_markdown(output, result) if output else None
    json_written = write_option_structure_outcome_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(option_structure_outcome_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_option_structure_outcome_report(
    settings: Settings,
    limit: int,
    symbol: str | None,
    price_source: str | None,
    json_output: bool,
) -> list[dict[str, Any]]:
    from recorder.analysis_database import AnalysisDatabase

    db = AnalysisDatabase(settings.database_path)
    rows = db.recent_option_structure_outcome_logs(limit=limit, symbol=symbol, price_source=price_source)
    if json_output:
        print(json.dumps({"option_structure_outcome_logs": rows}, indent=2, ensure_ascii=False))
        return rows
    print(json.dumps({"option_structure_outcome_logs": len(rows)}, indent=2, ensure_ascii=False))
    if not rows:
        print("\nNo option-structure outcome logs are stored yet.")
        return rows
    print("\nRecent option-structure outcome logs:")
    for row in rows:
        raw_return = row.get("raw_return")
        return_text = "-" if raw_return is None else f"{float(raw_return) * 100.0:.3f}%"
        print(
            f"#{row['id']} {row['symbol']} horizon={row['horizon']} gamma={row['gamma_regime'] or '-'} "
            f"return={return_text} direction={row['direction_label']} status={row['price_status']} "
            f"source={row['price_source']} blocker={row['blocker_reason'] or '-'}"
        )
    return rows


def command_single_stock_option_watchlist(
    settings: Settings,
    profile_json: Path | None,
    database_path: Path | None,
    execute_actions: bool,
    output: Path | None,
    json_output_path: Path | None,
    json_output: bool,
) -> dict[str, Any]:
    from recorder.single_stock_option_watchlist import (
        load_single_stock_option_watchlist_config,
        run_single_stock_option_watchlist,
        single_stock_option_watchlist_markdown,
        write_single_stock_option_watchlist_json,
        write_single_stock_option_watchlist_markdown,
    )

    config = load_single_stock_option_watchlist_config(profile_json, database_path=database_path or settings.database_path)
    result = run_single_stock_option_watchlist(config, cwd=Path.cwd(), execute_actions=execute_actions)
    written = write_single_stock_option_watchlist_markdown(output, result) if output else None
    json_written = write_single_stock_option_watchlist_json(json_output_path, result) if json_output_path else None
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return result
    print(single_stock_option_watchlist_markdown(result))
    if written:
        print(f"\nMarkdown report written: {written}")
    if json_written:
        print(f"JSON report written: {json_written}")
    return result


def command_single_stock_option_watchlist_loop(
    settings: Settings,
    profile_json: Path | None,
    database_path: Path | None,
    execute_actions: bool,
    output: Path | None,
    json_output_path: Path | None,
    interval_seconds: float,
    max_ticks: int,
) -> dict[str, Any]:
    from recorder.single_stock_option_watchlist import (
        load_single_stock_option_watchlist_config,
        run_single_stock_option_watchlist,
        write_single_stock_option_watchlist_json,
        write_single_stock_option_watchlist_markdown,
    )

    config = load_single_stock_option_watchlist_config(profile_json, database_path=database_path or settings.database_path)
    interval = max(15.0, float(interval_seconds))
    tick = 0
    last_result: dict[str, Any] | None = None
    print(
        json.dumps(
            {
                "status": "started",
                "command": "single-stock-option-watchlist-loop",
                "execute_actions": execute_actions,
                "interval_seconds": interval,
                "max_ticks": max_ticks,
                "safety": {
                    "read_only": True,
                    "ibkr_order_submitted": False,
                    "transmit_enabled": False,
                    "live_order_routing_enabled": False,
                },
            },
            ensure_ascii=False,
        )
    )
    while max_ticks <= 0 or tick < max_ticks:
        tick += 1
        try:
            result = run_single_stock_option_watchlist(config, cwd=Path.cwd(), execute_actions=execute_actions)
            last_result = result
            if output:
                write_single_stock_option_watchlist_markdown(output, result)
            if json_output_path:
                write_single_stock_option_watchlist_json(json_output_path, result)
            print(
                json.dumps(
                    {
                        "tick": tick,
                        "generated_at": result.get("generated_at"),
                        "status": result.get("status"),
                        "selected_symbols": result.get("selected_symbols"),
                        "action_status_counts": result.get("action_status_counts"),
                    },
                    ensure_ascii=False,
                )
            )
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "tick": tick,
                        "status": "error",
                        "error": str(exc),
                        "safety": {
                            "read_only": True,
                            "ibkr_order_submitted": False,
                            "transmit_enabled": False,
                            "live_order_routing_enabled": False,
                        },
                    },
                    ensure_ascii=False,
                )
            )
        if max_ticks > 0 and tick >= max_ticks:
            break
        time.sleep(interval)
    return last_result or {"status": "no_ticks", "tick_count": tick}
