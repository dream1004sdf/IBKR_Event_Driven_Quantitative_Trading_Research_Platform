from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable


EUROSTAT_INGESTION_VERSION = "eurostat-economic-indicators-v1"
KOSIS_INGESTION_VERSION = "kosis-economic-indicators-v1"
BOK_ECOS_INGESTION_VERSION = "bok-ecos-economic-indicators-v1"
ESTAT_JAPAN_INGESTION_VERSION = "estat-japan-economic-indicators-v1"
EUROSTAT_BASE_URL = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
KOSIS_BASE_URL = "https://kosis.kr/openapi/Param/statisticsParameterData.do"
BOK_ECOS_BASE_URL = "https://ecos.bok.or.kr/api/StatisticSearch"
ESTAT_BASE_URL = "https://api.e-stat.go.jp/rest/3.0/app/json/getStatsData"


@dataclass(frozen=True)
class EconomicIndicatorObservation:
    canonical_series_id: str
    provider: str
    native_series_id: str
    observation_date: str
    value: float
    unit: str = ""
    frequency: str = ""
    region: str = ""
    category: str = ""
    provider_priority: int = 20
    realtime_start: str = ""
    realtime_end: str = ""
    source_url: str = ""
    source_hash: str = ""
    ingestion_version: str = EUROSTAT_INGESTION_VERSION
    raw: dict[str, Any] | None = None


@dataclass(frozen=True)
class EurostatSeries:
    canonical_series_id: str
    label: str
    dataset: str
    params: dict[str, str]
    unit: str
    frequency: str
    region: str
    category: str
    provider_priority: int = 10


@dataclass(frozen=True)
class KosisSeries:
    canonical_series_id: str
    label: str
    org_id: str
    tbl_id: str
    item_id: str
    obj_l1: str = "ALL"
    obj_l2: str = ""
    obj_l3: str = ""
    prd_se: str = "M"
    unit: str = ""
    frequency: str = "M"
    region: str = "Korea"
    category: str = ""
    provider_priority: int = 10


@dataclass(frozen=True)
class BokEcosSeries:
    canonical_series_id: str
    label: str
    stat_code: str
    item_code1: str
    cycle: str = "M"
    item_code2: str = ""
    item_code3: str = ""
    item_code4: str = ""
    unit: str = ""
    frequency: str = "M"
    region: str = "Korea"
    category: str = ""
    provider_priority: int = 12


@dataclass(frozen=True)
class EstatJapanSeries:
    canonical_series_id: str
    label: str
    stats_data_id: str
    value_field: str = "$"
    dimension_params: dict[str, str] | None = None
    unit: str = ""
    frequency: str = "M"
    region: str = "Japan"
    category: str = ""
    provider_priority: int = 10


EUROSTAT_SERIES: tuple[EurostatSeries, ...] = (
    EurostatSeries(
        canonical_series_id="EUROSTAT_EA20_HICP_CP00",
        label="Euro area HICP total index direct from Eurostat",
        dataset="prc_hicp_midx",
        params={"freq": "M", "unit": "I15", "coicop": "CP00", "geo": "EA20"},
        unit="index_2015_100",
        frequency="M",
        region="Europe",
        category="inflation",
    ),
    EurostatSeries(
        canonical_series_id="EUROSTAT_EU27_UNRATE",
        label="EU unemployment rate direct from Eurostat",
        dataset="une_rt_m",
        params={"freq": "M", "s_adj": "SA", "age": "TOTAL", "unit": "PC_ACT", "sex": "T", "geo": "EU27_2020"},
        unit="percent_active_population",
        frequency="M",
        region="Europe",
        category="labor",
    ),
    EurostatSeries(
        canonical_series_id="EUROSTAT_EA20_INDUSTRIAL_PRODUCTION",
        label="Euro area industrial production direct from Eurostat",
        dataset="sts_inpr_m",
        params={"freq": "M", "indic_bt": "PRD", "nace_r2": "B-D", "s_adj": "SCA", "unit": "I21", "geo": "EA20"},
        unit="index_2021_100",
        frequency="M",
        region="Europe",
        category="manufacturing_industry",
    ),
)

KOSIS_SERIES: tuple[KosisSeries, ...] = (
    KosisSeries("KOSIS_KR_CPI", "Korea CPI direct from KOSIS", "", "", "", category="inflation", unit="index"),
    KosisSeries("KOSIS_KR_UNRATE", "Korea unemployment rate direct from KOSIS", "", "", "", category="labor", unit="percent"),
    KosisSeries("KOSIS_KR_INDUSTRIAL_PRODUCTION", "Korea industrial production direct from KOSIS", "", "", "", category="manufacturing_industry", unit="index"),
)

BOK_ECOS_SERIES: tuple[BokEcosSeries, ...] = (
    BokEcosSeries("BOK_KR_BASE_RATE", "Korea policy/base rate direct from BOK ECOS", "", "", category="policy_rates", unit="percent"),
    BokEcosSeries("BOK_KR_USDKRW", "KRW/USD exchange rate direct from BOK ECOS", "", "", category="fx", unit="krw_per_usd"),
)

ESTAT_JAPAN_SERIES: tuple[EstatJapanSeries, ...] = (
    EstatJapanSeries(
        "ESTAT_JP_CPI",
        "Japan CPI all items direct from e-Stat",
        "0003427113",
        dimension_params={"cdTab": "1", "cdCat01": "0001", "cdArea": "00000"},
        category="inflation",
        unit="index_2020_100",
    ),
    EstatJapanSeries(
        "ESTAT_JP_UNRATE",
        "Japan unemployment rate direct from e-Stat",
        "0003005865",
        dimension_params={"cdTab": "02", "cdCat01": "000", "cdCat02": "08", "cdCat03": "0", "cdArea": "00000"},
        category="labor",
        unit="percent",
    ),
    EstatJapanSeries("ESTAT_JP_INDUSTRIAL_PRODUCTION", "Japan industrial production direct from e-Stat/METI", "", category="manufacturing_industry", unit="index"),
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_date(value: str) -> date:
    text = str(value or "").strip()
    if len(text) == 7:
        return datetime.strptime(f"{text}-01", "%Y-%m-%d").date()
    return datetime.strptime(text[:10], "%Y-%m-%d").date()


def _month_span(start: str | None, end: str | None) -> int:
    if not start or not end:
        return 48
    start_date = _parse_date(start)
    end_date = _parse_date(end)
    months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
    return max(12, months + 12)


class EurostatClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(1, int(retries))
        self.opener = opener or urllib.request.urlopen

    def fetch_series(
        self,
        series: EurostatSeries,
        *,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[dict[str, Any], str]:
        params = {
            "lang": "en",
            **series.params,
            "lastTimePeriod": str(_month_span(start, end)),
        }
        url = f"{EUROSTAT_BASE_URL}/{series.dataset}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        last_error: Exception | None = None
        for _attempt in range(self.retries):
            try:
                response = self.opener(request, timeout=self.timeout_seconds)
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
                if not isinstance(payload, dict):
                    raise RuntimeError("Eurostat response was not a JSON object")
                return payload, url
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
        raise RuntimeError(f"Eurostat request failed after {self.retries} attempt(s): {last_error}") from last_error


def _time_index(payload: dict[str, Any]) -> dict[int, str]:
    time_dim = ((payload.get("dimension") or {}).get("time") or {}).get("category") or {}
    index = time_dim.get("index") or {}
    if not isinstance(index, dict):
        return {}
    return {int(position): str(period) for period, position in index.items()}


def _parse_observations(
    series: EurostatSeries,
    payload: dict[str, Any],
    *,
    source_url: str,
    start: str | None = None,
    end: str | None = None,
) -> list[EconomicIndicatorObservation]:
    start_date = _parse_date(start) if start else None
    end_date = _parse_date(end) if end else None
    time_by_position = _time_index(payload)
    values = payload.get("value") or {}
    if not isinstance(values, dict):
        return []
    rows: list[EconomicIndicatorObservation] = []
    for raw_index, raw_value in values.items():
        try:
            position = int(raw_index)
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        period = time_by_position.get(position)
        if not period:
            continue
        observation_date = _parse_date(period)
        if start_date and observation_date < start_date:
            continue
        if end_date and observation_date > end_date:
            continue
        observation_iso = observation_date.isoformat()
        rows.append(
            EconomicIndicatorObservation(
                canonical_series_id=series.canonical_series_id,
                provider="eurostat",
                native_series_id=f"{series.dataset}:{urllib.parse.urlencode(series.params)}",
                observation_date=observation_iso,
                value=value,
                unit=series.unit,
                frequency=series.frequency,
                region=series.region,
                category=series.category,
                provider_priority=series.provider_priority,
                realtime_start=observation_iso,
                realtime_end=observation_iso,
                source_url=source_url,
                source_hash=_sha256(f"{series.canonical_series_id}|{observation_iso}|{value}"),
                raw={"label": series.label, "dataset": series.dataset, "params": series.params},
            )
        )
    rows.sort(key=lambda item: item.observation_date, reverse=True)
    return rows


class EurostatCollector:
    def __init__(self, database: Any, client: EurostatClient) -> None:
        self.database = database
        self.client = client

    def sync(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        series_ids: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        requested = {item.strip().upper() for item in (series_ids or ()) if item.strip()}
        series_list = tuple(
            series for series in EUROSTAT_SERIES
            if not requested or series.canonical_series_id.upper() in requested
        )
        summary: dict[str, Any] = {
            "ingestion_version": EUROSTAT_INGESTION_VERSION,
            "provider": "eurostat",
            "series_count": len(series_list),
            "stored": 0,
            "updated": 0,
            "errors": 0,
            "results": [],
        }
        for series in series_list:
            item = {
                "series_id": series.canonical_series_id,
                "dataset": series.dataset,
                "observations": 0,
                "stored": 0,
                "updated": 0,
                "error": None,
            }
            try:
                payload, source_url = self.client.fetch_series(series, start=start, end=end)
                observations = _parse_observations(series, payload, source_url=source_url, start=start, end=end)
                item["observations"] = len(observations)
                for observation in observations:
                    _, created = self.database.upsert_economic_indicator_observation(observation)
                    if created:
                        summary["stored"] += 1
                        item["stored"] += 1
                    else:
                        summary["updated"] += 1
                        item["updated"] += 1
            except Exception as exc:
                summary["errors"] += 1
                item["error"] = str(exc)
            summary["results"].append(item)
        return summary


def _yyyymm(value: str | None) -> str:
    if not value:
        return ""
    parsed = _parse_date(value)
    return f"{parsed.year:04d}{parsed.month:02d}"


def _parse_kosis_date(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-01"
    if len(text) == 4 and text.isdigit():
        return f"{text}-01-01"
    return _parse_date(text).isoformat()


def _series_result(series_id: str, *, dataset: str = "", error: str | None = None) -> dict[str, Any]:
    return {
        "series_id": series_id,
        "dataset": dataset,
        "observations": 0,
        "stored": 0,
        "updated": 0,
        "skipped": 0,
        "error": error,
    }


class KosisClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(1, int(retries))
        self.opener = opener or urllib.request.urlopen

    def fetch_series(self, series: KosisSeries, *, start: str | None = None, end: str | None = None) -> tuple[Any, str]:
        if not self.api_key:
            raise RuntimeError("KOSIS_API_KEY is not configured")
        if not (series.org_id and series.tbl_id and series.item_id):
            raise RuntimeError("KOSIS series native config is incomplete; set org_id, tbl_id, and item_id")
        params = {
            "method": "getList",
            "apiKey": self.api_key,
            "format": "json",
            "jsonVD": "Y",
            "prdSe": series.prd_se,
            "startPrdDe": _yyyymm(start),
            "endPrdDe": _yyyymm(end),
            "orgId": series.org_id,
            "tblId": series.tbl_id,
            "itmId": series.item_id,
            "objL1": series.obj_l1,
        }
        if series.obj_l2:
            params["objL2"] = series.obj_l2
        if series.obj_l3:
            params["objL3"] = series.obj_l3
        params = {key: value for key, value in params.items() if value}
        url = f"{KOSIS_BASE_URL}?{urllib.parse.urlencode(params)}"
        safe_params = dict(params)
        safe_params["apiKey"] = "REDACTED"
        safe_url = f"{KOSIS_BASE_URL}?{urllib.parse.urlencode(safe_params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        last_error: Exception | None = None
        for _attempt in range(self.retries):
            try:
                response = self.opener(request, timeout=self.timeout_seconds)
                return json.loads(response.read().decode("utf-8", errors="replace")), safe_url
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
        raise RuntimeError(f"KOSIS request failed after {self.retries} attempt(s): {last_error}") from last_error


def _parse_kosis_observations(
    series: KosisSeries,
    payload: Any,
    *,
    source_url: str,
    start: str | None = None,
    end: str | None = None,
) -> list[EconomicIndicatorObservation]:
    rows_payload = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
    start_date = _parse_date(start) if start else None
    end_date = _parse_date(end) if end else None
    rows: list[EconomicIndicatorObservation] = []
    for row in rows_payload:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("PRD_DE") or row.get("prd_de") or row.get("time") or "").strip()
        raw_value = row.get("DT", row.get("dt", row.get("value")))
        if not raw_date or raw_value in (None, ""):
            continue
        try:
            observation_date = _parse_date(_parse_kosis_date(raw_date))
            value = float(str(raw_value).replace(",", ""))
        except ValueError:
            continue
        if start_date and observation_date < start_date:
            continue
        if end_date and observation_date > end_date:
            continue
        observation_iso = observation_date.isoformat()
        rows.append(
            EconomicIndicatorObservation(
                canonical_series_id=series.canonical_series_id,
                provider="kosis",
                native_series_id=f"{series.org_id}:{series.tbl_id}:{series.item_id}:{series.obj_l1}",
                observation_date=observation_iso,
                value=value,
                unit=series.unit or str(row.get("UNIT_NM") or row.get("unit") or ""),
                frequency=series.frequency,
                region=series.region,
                category=series.category,
                provider_priority=series.provider_priority,
                realtime_start=observation_iso,
                realtime_end=observation_iso,
                source_url=source_url,
                source_hash=_sha256(f"{series.canonical_series_id}|{observation_iso}|{value}"),
                ingestion_version=KOSIS_INGESTION_VERSION,
                raw={"label": series.label, "row": row},
            )
        )
    rows.sort(key=lambda item: item.observation_date, reverse=True)
    return rows


class KosisCollector:
    def __init__(self, database: Any, client: KosisClient) -> None:
        self.database = database
        self.client = client

    def sync(self, *, start: str | None = None, end: str | None = None, series_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
        return _sync_series_collection(
            database=self.database,
            provider="kosis",
            ingestion_version=KOSIS_INGESTION_VERSION,
            series_list=KOSIS_SERIES,
            requested=series_ids,
            fetch=lambda series: self.client.fetch_series(series, start=start, end=end),
            parse=lambda series, payload, url: _parse_kosis_observations(series, payload, source_url=url, start=start, end=end),
        )


class BokEcosClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(1, int(retries))
        self.opener = opener or urllib.request.urlopen

    def fetch_series(self, series: BokEcosSeries, *, start: str | None = None, end: str | None = None) -> tuple[Any, str]:
        if not self.api_key:
            raise RuntimeError("BOK_ECOS_API_KEY is not configured")
        if not (series.stat_code and series.item_code1):
            raise RuntimeError("BOK ECOS series native config is incomplete; set stat_code and item_code1")
        start_value = _yyyymm(start) or "202501"
        end_value = _yyyymm(end) or start_value
        path_parts = [
            self.api_key,
            "json",
            "en",
            "1",
            "10000",
            series.stat_code,
            series.cycle,
            start_value,
            end_value,
            series.item_code1,
        ]
        for code in (series.item_code2, series.item_code3, series.item_code4):
            if code:
                path_parts.append(code)
        url = f"{BOK_ECOS_BASE_URL}/{'/'.join(path_parts)}"
        safe_url = url.replace(self.api_key, "REDACTED", 1)
        request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        last_error: Exception | None = None
        for _attempt in range(self.retries):
            try:
                response = self.opener(request, timeout=self.timeout_seconds)
                return json.loads(response.read().decode("utf-8", errors="replace")), safe_url
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
        raise RuntimeError(f"BOK ECOS request failed after {self.retries} attempt(s): {last_error}") from last_error


def _parse_bok_date(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 6 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-01"
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return _parse_kosis_date(text)


def _parse_bok_observations(series: BokEcosSeries, payload: Any, *, source_url: str, start: str | None = None, end: str | None = None) -> list[EconomicIndicatorObservation]:
    rows_payload = (((payload or {}).get("StatisticSearch") or {}).get("row") or []) if isinstance(payload, dict) else []
    start_date = _parse_date(start) if start else None
    end_date = _parse_date(end) if end else None
    rows: list[EconomicIndicatorObservation] = []
    for row in rows_payload:
        if not isinstance(row, dict):
            continue
        raw_date = str(row.get("TIME") or "").strip()
        raw_value = row.get("DATA_VALUE")
        if not raw_date or raw_value in (None, ""):
            continue
        try:
            observation_date = _parse_date(_parse_bok_date(raw_date))
            value = float(str(raw_value).replace(",", ""))
        except ValueError:
            continue
        if start_date and observation_date < start_date:
            continue
        if end_date and observation_date > end_date:
            continue
        observation_iso = observation_date.isoformat()
        rows.append(
            EconomicIndicatorObservation(
                canonical_series_id=series.canonical_series_id,
                provider="bok_ecos",
                native_series_id=f"{series.stat_code}:{series.item_code1}:{series.item_code2}:{series.item_code3}:{series.item_code4}",
                observation_date=observation_iso,
                value=value,
                unit=series.unit or str(row.get("UNIT_NAME") or ""),
                frequency=series.frequency,
                region=series.region,
                category=series.category,
                provider_priority=series.provider_priority,
                realtime_start=observation_iso,
                realtime_end=observation_iso,
                source_url=source_url,
                source_hash=_sha256(f"{series.canonical_series_id}|{observation_iso}|{value}"),
                ingestion_version=BOK_ECOS_INGESTION_VERSION,
                raw={"label": series.label, "row": row},
            )
        )
    rows.sort(key=lambda item: item.observation_date, reverse=True)
    return rows


class BokEcosCollector:
    def __init__(self, database: Any, client: BokEcosClient) -> None:
        self.database = database
        self.client = client

    def sync(self, *, start: str | None = None, end: str | None = None, series_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
        return _sync_series_collection(
            database=self.database,
            provider="bok_ecos",
            ingestion_version=BOK_ECOS_INGESTION_VERSION,
            series_list=BOK_ECOS_SERIES,
            requested=series_ids,
            fetch=lambda series: self.client.fetch_series(series, start=start, end=end),
            parse=lambda series, payload, url: _parse_bok_observations(series, payload, source_url=url, start=start, end=end),
        )


class EstatJapanClient:
    def __init__(
        self,
        *,
        app_id: str | None,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.app_id = str(app_id or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(1, int(retries))
        self.opener = opener or urllib.request.urlopen

    def fetch_series(self, series: EstatJapanSeries, *, start: str | None = None, end: str | None = None) -> tuple[Any, str]:
        if not self.app_id:
            raise RuntimeError("ESTAT_APP_ID is not configured")
        if not series.stats_data_id:
            raise RuntimeError("Japan e-Stat series native config is incomplete; set stats_data_id and dimension_params")
        params = {
            "appId": self.app_id,
            "lang": "E",
            "statsDataId": series.stats_data_id,
            "metaGetFlg": "N",
            "cntGetFlg": "N",
            "explanationGetFlg": "N",
            "annotationGetFlg": "N",
            "limit": "10000",
        }
        if start:
            params["startPosition"] = "1"
        for key, value in (series.dimension_params or {}).items():
            if value:
                params[key] = value
        url = f"{ESTAT_BASE_URL}?{urllib.parse.urlencode(params)}"
        safe_params = dict(params)
        safe_params["appId"] = "REDACTED"
        safe_url = f"{ESTAT_BASE_URL}?{urllib.parse.urlencode(safe_params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        last_error: Exception | None = None
        for _attempt in range(self.retries):
            try:
                response = self.opener(request, timeout=self.timeout_seconds)
                return json.loads(response.read().decode("utf-8", errors="replace")), safe_url
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
                last_error = exc
        raise RuntimeError(f"e-Stat Japan request failed after {self.retries} attempt(s): {last_error}") from last_error


def _estat_values(payload: Any) -> list[dict[str, Any]]:
    values = (((payload or {}).get("GET_STATS_DATA") or {}).get("STATISTICAL_DATA") or {}).get("DATA_INF", {}).get("VALUE", [])
    if isinstance(values, dict):
        return [values]
    return values if isinstance(values, list) else []


def _parse_estat_date(value: str) -> str:
    text = str(value or "").strip()
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and digits[4:6] == "00":
        return f"{digits[:4]}-{digits[6:8]}-01"
    if len(digits) >= 6 and digits[4:6] != "00":
        return f"{digits[:4]}-{digits[4:6]}-01"
    if len(digits) >= 4:
        return f"{digits[:4]}-01-01"
    return _parse_kosis_date(text)


def _parse_estat_observations(series: EstatJapanSeries, payload: Any, *, source_url: str, start: str | None = None, end: str | None = None) -> list[EconomicIndicatorObservation]:
    start_date = _parse_date(start) if start else None
    end_date = _parse_date(end) if end else None
    rows: list[EconomicIndicatorObservation] = []
    for row in _estat_values(payload):
        raw_date = str(row.get("@time") or row.get("time") or "").strip()
        raw_value = row.get(series.value_field, row.get("$"))
        if not raw_date or raw_value in (None, ""):
            continue
        try:
            observation_date = _parse_date(_parse_estat_date(raw_date))
            value = float(str(raw_value).replace(",", ""))
        except ValueError:
            continue
        if start_date and observation_date < start_date:
            continue
        if end_date and observation_date > end_date:
            continue
        observation_iso = observation_date.isoformat()
        rows.append(
            EconomicIndicatorObservation(
                canonical_series_id=series.canonical_series_id,
                provider="estat_japan",
                native_series_id=f"{series.stats_data_id}:{urllib.parse.urlencode(series.dimension_params or {})}",
                observation_date=observation_iso,
                value=value,
                unit=series.unit,
                frequency=series.frequency,
                region=series.region,
                category=series.category,
                provider_priority=series.provider_priority,
                realtime_start=observation_iso,
                realtime_end=observation_iso,
                source_url=source_url,
                source_hash=_sha256(f"{series.canonical_series_id}|{observation_iso}|{value}"),
                ingestion_version=ESTAT_JAPAN_INGESTION_VERSION,
                raw={"label": series.label, "row": row},
            )
        )
    rows.sort(key=lambda item: item.observation_date, reverse=True)
    return rows


class EstatJapanCollector:
    def __init__(self, database: Any, client: EstatJapanClient) -> None:
        self.database = database
        self.client = client

    def sync(self, *, start: str | None = None, end: str | None = None, series_ids: tuple[str, ...] | None = None) -> dict[str, Any]:
        return _sync_series_collection(
            database=self.database,
            provider="estat_japan",
            ingestion_version=ESTAT_JAPAN_INGESTION_VERSION,
            series_list=ESTAT_JAPAN_SERIES,
            requested=series_ids,
            fetch=lambda series: self.client.fetch_series(series, start=start, end=end),
            parse=lambda series, payload, url: _parse_estat_observations(series, payload, source_url=url, start=start, end=end),
        )


def _sync_series_collection(
    *,
    database: Any,
    provider: str,
    ingestion_version: str,
    series_list: tuple[Any, ...],
    requested: tuple[str, ...] | None,
    fetch: Callable[[Any], tuple[Any, str]],
    parse: Callable[[Any, Any, str], list[EconomicIndicatorObservation]],
) -> dict[str, Any]:
    requested_set = {item.strip().upper() for item in (requested or ()) if item.strip()}
    selected = tuple(series for series in series_list if not requested_set or series.canonical_series_id.upper() in requested_set)
    summary: dict[str, Any] = {
        "ingestion_version": ingestion_version,
        "provider": provider,
        "series_count": len(selected),
        "stored": 0,
        "updated": 0,
        "skipped": 0,
        "errors": 0,
        "results": [],
    }
    for series in selected:
        item = _series_result(series.canonical_series_id, dataset=getattr(series, "tbl_id", "") or getattr(series, "stat_code", "") or getattr(series, "stats_data_id", ""))
        try:
            payload, source_url = fetch(series)
            observations = parse(series, payload, source_url)
            item["observations"] = len(observations)
            for observation in observations:
                _, created = database.upsert_economic_indicator_observation(observation)
                if created:
                    summary["stored"] += 1
                    item["stored"] += 1
                else:
                    summary["updated"] += 1
                    item["updated"] += 1
        except Exception as exc:
            message = str(exc)
            if "is not configured" in message or "native config is incomplete" in message:
                summary["skipped"] += 1
                item["skipped"] = 1
                item["error"] = f"skipped: {message}"
            else:
                summary["errors"] += 1
                item["error"] = message
        summary["results"].append(item)
    return summary
