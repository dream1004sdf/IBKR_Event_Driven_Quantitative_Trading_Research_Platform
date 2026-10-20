from __future__ import annotations

from recorder.source_operations import (
    SourceLiveValidationConfig,
    build_source_live_validation_report,
    source_live_validation_markdown,
)


class SettingsStub:
    client_id = 21
    host = "127.0.0.1"
    port = 4001
    marketaux_api_key = "test-marketaux"
    alpha_vantage_api_key = "test-alpha"


def test_source_live_validation_rolls_up_provider_statuses(monkeypatch) -> None:
    import recorder.source_operations as module

    class ProbeStub:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run_probe(self):
            return {
                "provider": "ibkr_historical_news",
                "source_key": "market_news",
                "reachable": True,
                "status": "no_rows",
                "row_count": 0,
                "latest_event_at": None,
                "latest_event_age_minutes": None,
                "errors": [],
            }

    monkeypatch.setattr(module, "IBKRNewsProbe", ProbeStub)
    monkeypatch.setattr(
        module,
        "validate_marketaux",
        lambda **kwargs: {
            "provider": "marketaux",
            "source_key": "geopolitical_signals",
            "reachable": True,
            "status": "provider_notice",
            "row_count": 0,
            "latest_event_at": None,
            "latest_event_age_minutes": None,
            "errors": ["rate limit"],
        },
    )
    monkeypatch.setattr(
        module,
        "validate_alpha_vantage",
        lambda **kwargs: {
            "provider": "alpha_vantage",
            "source_key": "geopolitical_signals",
            "reachable": True,
            "status": "ok",
            "row_count": 2,
            "latest_event_at": "2026-06-30T09:59:00+00:00",
            "latest_event_age_minutes": 2.0,
            "errors": [],
        },
    )

    report = build_source_live_validation_report(
        settings=SettingsStub(),
        config=SourceLiveValidationConfig(
            symbols=("SPY",),
            providers=("BZ",),
            external_symbols=("SPY",),
            alpha_tickers=("SPY",),
            include_alpha_vantage=True,
            limit=5,
        ),
    )
    markdown = source_live_validation_markdown(report)

    assert report["source_live_validation_version"] == "source-live-validation-v4"
    assert report["overall_status"] == "yellow"
    assert report["source_status"]["geopolitical_signals"] == "ok"
    assert report["source_status"]["market_news"] == "no_rows"
    assert report["provider_status_counts"]["ok"] == 1
    assert "IBKR" not in markdown  # provider names stay exact/lowercase in rows
    assert "source-live-validation-v4" in markdown


def test_source_live_validation_treats_ibkr_transient_farm_warning_as_ok(monkeypatch) -> None:
    import recorder.source_operations as module

    class ProbeStub:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def run_probe(self):
            return {
                "provider": "ibkr_historical_news",
                "source_key": "market_news",
                "reachable": True,
                "status": "ok_with_transient_warnings",
                "row_count": 50,
                "latest_event_at": "2026-06-30T09:59:00+00:00",
                "latest_event_age_minutes": 2.0,
                "transient_warning_count": 1,
                "errors": ["IBKR message reqId=-1 details=(2103, 'Market data farm connection is broken:usfuture')"],
            }

    monkeypatch.setattr(module, "IBKRNewsProbe", ProbeStub)
    monkeypatch.setattr(module, "validate_marketaux", lambda **kwargs: {"provider": "marketaux", "source_key": "geopolitical_signals", "reachable": True, "status": "ok", "row_count": 1, "latest_event_at": "2026-06-30T09:59:00+00:00", "latest_event_age_minutes": 2.0, "errors": []})
    monkeypatch.setattr(module, "validate_alpha_vantage", lambda **kwargs: {"provider": "alpha_vantage", "source_key": "geopolitical_signals", "reachable": True, "status": "provider_notice", "row_count": 0, "latest_event_at": None, "latest_event_age_minutes": None, "errors": ["rate limit"]})

    report = build_source_live_validation_report(
        settings=SettingsStub(),
        config=SourceLiveValidationConfig(
            symbols=("SPY",),
            providers=("BZ",),
            external_symbols=("SPY",),
            alpha_tickers=("SPY",),
            limit=5,
        ),
    )

    assert report["source_status"]["market_news"] == "ok"
    assert report["providers"][0]["status"] == "ok_with_transient_warnings"
    assert report["provider_status_counts"]["ok_with_transient_warnings"] == 1


def test_source_live_validation_retries_marketaux_timeout_once(monkeypatch) -> None:
    import recorder.source_operations as module

    calls = {"count": 0}

    class MarketauxClientStub:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def fetch(self, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise TimeoutError("temporary timeout")
            return {"data": [{"published_at": module._iso_now()}]}

    monkeypatch.setattr(module, "MarketauxClient", MarketauxClientStub)

    row = module.validate_marketaux(
        api_key="key",
        symbols=(),
        limit=5,
        timeout_seconds=1.0,
        retry_count=1,
        retry_sleep_seconds=0.0,
        search=None,
        filter_entities=False,
    )

    assert calls["count"] == 2
    assert row["status"] == "ok"
    assert row["attempt_count"] == 2
    assert row["row_count"] == 1
