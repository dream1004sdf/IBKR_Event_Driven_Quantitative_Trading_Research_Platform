from __future__ import annotations

import json
from datetime import date, timedelta
from types import SimpleNamespace

from recorder.analysis_database import AnalysisDatabase
from recorder.economic_indicator_desk import build_economic_indicator_desk, economic_indicator_desk_markdown
from recorder.fred_sources import FredCollector, FredCsvClient
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
from recorder.macro_universe import metadata_by_series, series_ids_for_universe
from recorder.market_outcomes import DailyBar
from recorder.sec_financials import SecCompanyFactsClient, SecFinancialFactsCollector
from recorder.technical_indicators import TechnicalIndicatorBuilder


class _Response:
    def __init__(self, payload: str | bytes):
        self.payload = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.headers = {"Content-Type": "application/json"}

    def read(self, _size: int = -1):
        return self.payload


def test_sec_financial_facts_sync_stores_companyfacts(tmp_path):
    ticker_json = '{"0":{"cik_str":320193,"ticker":"AAPL","title":"Apple Inc."}}'
    facts_json = """
    {
      "facts": {
        "us-gaap": {
          "Revenues": {
            "label": "Revenues",
            "units": {
              "USD": [
                {
                  "fy": 2025,
                  "fp": "FY",
                  "form": "10-K",
                  "filed": "2025-10-31",
                  "start": "2024-09-29",
                  "end": "2025-09-27",
                  "val": 391035000000,
                  "accn": "0000320193-25-000079",
                  "frame": "CY2025"
                }
              ]
            }
          }
        }
      }
    }
    """

    def opener(request, timeout=20):
        url = request.full_url
        if "company_tickers" in url:
            return _Response(ticker_json)
        if "companyfacts" in url:
            return _Response(facts_json)
        raise AssertionError(url)

    db = AnalysisDatabase(tmp_path / "financial-facts.db")
    client = SecCompanyFactsClient(user_agent="Tester tester@example.com", opener=opener)
    result = SecFinancialFactsCollector(db, client).sync_symbols(("AAPL",), tags=("Revenues",), limit_per_symbol=10)
    rows = db.recent_financial_statement_facts(symbol="AAPL", tag="Revenues", limit=5)

    assert result["stored"] == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["tag"] == "Revenues"
    assert rows[0]["value"] == 391035000000


def test_fred_sync_stores_csv_observations(tmp_path):
    csv_payload = "observation_date,FEDFUNDS\n2026-01-01,4.33\n2026-02-01,4.33\n"

    def opener(request, timeout=20):
        assert "fredgraph.csv" in request.full_url
        return _Response(csv_payload)

    db = AnalysisDatabase(tmp_path / "fred.db")
    result = FredCollector(db, FredCsvClient(opener=opener)).sync(("FEDFUNDS",), limit_per_series=10)
    rows = db.recent_fred_observations(series_id="FEDFUNDS", limit=5)

    assert result["stored"] == 2
    assert rows[0]["series_id"] == "FEDFUNDS"
    assert rows[0]["observation_date"] == "2026-02-01"
    assert rows[0]["value"] == 4.33


def test_fred_sync_uses_api_key_without_storing_secret(tmp_path):
    api_payload = """
    {
      "observations": [
        {
          "realtime_start": "2026-02-01",
          "realtime_end": "2026-02-01",
          "date": "2026-02-01",
          "value": "4.33"
        }
      ]
    }
    """

    def opener(request, timeout=20):
        assert "api.stlouisfed.org" in request.full_url
        assert "api_key=test-secret" in request.full_url
        return _Response(api_payload)

    db = AnalysisDatabase(tmp_path / "fred-api.db")
    result = FredCollector(db, FredCsvClient(api_key="test-secret", opener=opener)).sync(
        ("FEDFUNDS",),
        limit_per_series=10,
    )
    rows = db.recent_fred_observations(series_id="FEDFUNDS", limit=5)

    assert result["stored"] == 1
    assert rows[0]["series_id"] == "FEDFUNDS"
    assert rows[0]["value"] == 4.33
    assert "test-secret" not in rows[0]["source_url"]
    assert "api_key=REDACTED" in rows[0]["source_url"]


def test_fred_global_macro_universe_covers_labor_surveys_and_international_series():
    series_ids = series_ids_for_universe("global_core")
    metadata = metadata_by_series("global_core")

    assert "UNRATE" in series_ids
    assert "ICSA" in series_ids
    assert "UMCSENT" in series_ids
    assert "MICH" in series_ids
    assert "LRUN64TTJPM156S" in series_ids
    assert "JPNPRINTO01GYSAM" in series_ids
    assert "LRUNTTTTKRM156S" in series_ids
    assert "KORPRINTO01GYSAM" in series_ids
    assert "LRHUTTTTEZM156S" in series_ids
    assert "CP0000EZ19M086NEST" in series_ids
    assert "ESTAT_JP_CPI" in series_ids
    assert "ESTAT_JP_UNRATE" in series_ids
    assert "IRLTLT01JPM156N" in series_ids
    assert "IRLTLT01KRM156N" in series_ids
    assert "ECBDFR" in series_ids
    assert "ECBMRRFR" in series_ids
    assert "IRLTLT01DEM156N" in series_ids
    assert "IUDSOIA" in series_ids
    assert "IRLTLT01GBM156N" in series_ids
    assert metadata["ICSA"].category == "labor_high_frequency"
    assert metadata["UMCSENT"].source_family.startswith("University of Michigan")
    assert metadata["LRUNTTTTKRM156S"].region == "Korea"
    assert metadata["ESTAT_JP_CPI"].source_family == "Japan e-Stat direct"
    assert metadata["ECBDFR"].category == "policy_rates"
    assert metadata["IUDSOIA"].region == "UK"


def test_economic_indicator_desk_summarizes_cutoff_safe_macro_flags(tmp_path):
    db = AnalysisDatabase(tmp_path / "economic-indicator-desk.db")
    for series_id, observation_date, value in [
        ("DGS10", "2026-06-18", 4.50),
        ("DGS10", "2026-06-19", 4.70),
        ("DGS10", "2026-06-25", 5.20),
        ("DGS2", "2026-06-18", 4.70),
        ("DGS2", "2026-06-19", 4.95),
        ("DGS3MO", "2026-06-19", 5.10),
        ("ICSA", "2026-06-12", 220000),
        ("ICSA", "2026-06-19", 245000),
        ("MICH", "2026-06-01", 3.4),
    ]:
        db.upsert_fred_observation(
            SimpleNamespace(
                series_id=series_id,
                observation_date=observation_date,
                value=value,
                realtime_start=observation_date,
                realtime_end=observation_date,
                source_url=f"https://fred.test/{series_id}",
                source_hash=f"{series_id}-{observation_date}",
                ingestion_version="fixture",
            )
        )

    desk = build_economic_indicator_desk(db, cutoff_date="2026-06-20", universe="global_core")
    dgs10 = next(row for row in desk["indicators"] if row["series_id"] == "DGS10")
    flags = {row["flag"] for row in desk["macro_flags"]}
    report = economic_indicator_desk_markdown(desk)

    assert dgs10["latest_date"] == "2026-06-19"
    assert dgs10["latest_value"] == 4.70
    assert "yield_curve_inverted_2s10s" in flags
    assert "yield_curve_inverted_3m10y" in flags
    assert "labor_softening" in flags
    assert desk["risk_bias"] == "macro_risk_off"
    assert "2026-06-25" not in report
    assert "Rates/curve desk is restrictive" in report


def test_eurostat_direct_sync_stores_provider_neutral_observations_and_feeds_desk(tmp_path):
    payload = json.dumps(
        {
            "version": "2.0",
            "class": "dataset",
            "id": ["freq", "unit", "coicop", "geo", "time"],
            "size": [1, 1, 1, 1, 2],
            "dimension": {
                "time": {
                    "category": {
                        "index": {
                            "2026-04": 0,
                            "2026-05": 1,
                        }
                    }
                }
            },
            "value": {"0": 103.0, "1": 103.5},
        }
    )

    def opener(request, timeout=20):
        assert "eurostat" in request.full_url
        assert "prc_hicp_midx" in request.full_url
        return _Response(payload)

    db = AnalysisDatabase(tmp_path / "eurostat.db")
    collector = EurostatCollector(db, EurostatClient(opener=opener))
    result = collector.sync(
        start="2026-04-01",
        end="2026-05-31",
        series_ids=("EUROSTAT_EA20_HICP_CP00",),
    )
    rows = db.recent_economic_indicator_observations(
        canonical_series_id="EUROSTAT_EA20_HICP_CP00",
        provider="eurostat",
        limit=5,
    )
    desk = build_economic_indicator_desk(db, cutoff_date="2026-05-31", universe="global_core")
    direct = next(row for row in desk["indicators"] if row["series_id"] == "EUROSTAT_EA20_HICP_CP00")

    assert result["stored"] == 2
    assert rows[0]["provider"] == "eurostat"
    assert rows[0]["observation_date"] == "2026-05-01"
    assert rows[0]["value"] == 103.5
    assert direct["provider"] == "eurostat"
    assert direct["latest_value"] == 103.5
    assert desk["provider_counts"]["eurostat"] == 1


def test_korea_japan_direct_collectors_skip_when_keys_or_native_mappings_missing(tmp_path):
    db = AnalysisDatabase(tmp_path / "missing-direct-keys.db")

    kosis = KosisCollector(db, KosisClient(api_key=None)).sync(start="2026-01-01", end="2026-06-30")
    bok = BokEcosCollector(db, BokEcosClient(api_key=None)).sync(start="2026-01-01", end="2026-06-30")
    estat = EstatJapanCollector(db, EstatJapanClient(app_id=None)).sync(start="2026-01-01", end="2026-06-30")

    assert kosis["provider"] == "kosis"
    assert kosis["skipped"] == 3
    assert "KOSIS_API_KEY is not configured" in kosis["results"][0]["error"]
    assert bok["provider"] == "bok_ecos"
    assert bok["skipped"] == 2
    assert "BOK_ECOS_API_KEY is not configured" in bok["results"][0]["error"]
    assert estat["provider"] == "estat_japan"
    assert estat["skipped"] == 3
    assert "ESTAT_APP_ID is not configured" in estat["results"][0]["error"]


class _FixtureDailySource:
    name = "fixture_daily_close"

    def get_daily_bars(self, symbol: str, start: date, end: date):
        rows = []
        cursor = start
        value = 100.0
        while cursor <= end:
            rows.append(DailyBar(symbol=symbol, date=cursor, close=value))
            value += 1.0
            cursor += timedelta(days=1)
        return rows


def test_technical_indicator_builder_stores_daily_features(tmp_path):
    db = AnalysisDatabase(tmp_path / "technical.db")
    builder = TechnicalIndicatorBuilder(db, _FixtureDailySource())
    result = builder.build(
        symbols=("MSFT",),
        start=date(2026, 3, 1),
        end=date(2026, 3, 31),
        lookback_days=90,
        limit_per_symbol=100,
    )
    rows = db.recent_technical_indicators(symbol="MSFT", price_source="fixture_daily_close", limit=5)

    assert result["stored"] == 31
    assert rows[0]["symbol"] == "MSFT"
    assert rows[0]["as_of_date"] == "2026-03-31"
    assert rows[0]["sma_20"] is not None
    assert rows[0]["ema_12"] is not None
    assert rows[0]["macd"] is not None
    assert rows[0]["rsi_14"] is not None
