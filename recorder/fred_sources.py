from __future__ import annotations

import csv
import hashlib
import io
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

FRED_INGESTION_VERSION = "fred-observations-v1"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API_OBSERVATIONS_URL = "https://api.stlouisfed.org/fred/series/observations"


@dataclass(frozen=True)
class FredObservation:
    series_id: str
    observation_date: str
    value: float
    realtime_start: str = ""
    realtime_end: str = ""
    source_url: str = ""
    source_hash: str = ""
    ingestion_version: str = FRED_INGESTION_VERSION


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FredCsvClient:
    def __init__(
        self,
        *,
        timeout_seconds: float = 45.0,
        retries: int = 2,
        api_key: str | None = None,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self.retries = max(1, int(retries))
        self.api_key = str(api_key or "").strip()
        self.opener = opener or urllib.request.urlopen

    def fetch_api_observations(
        self,
        series_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 5000,
    ) -> tuple[list[dict[str, Any]], str]:
        if not self.api_key:
            raise RuntimeError("FRED API key is not configured")
        normalized = series_id.strip().upper()
        params = {
            "series_id": normalized,
            "api_key": self.api_key,
            "file_type": "json",
            "sort_order": "desc",
            "limit": str(max(1, int(limit))),
        }
        if start:
            params["observation_start"] = start
        if end:
            params["observation_end"] = end
        request_url = f"{FRED_API_OBSERVATIONS_URL}?{urllib.parse.urlencode(params)}"
        safe_params = dict(params)
        safe_params["api_key"] = "REDACTED"
        safe_url = f"{FRED_API_OBSERVATIONS_URL}?{urllib.parse.urlencode(safe_params)}"
        request = urllib.request.Request(request_url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        last_error: Exception | None = None
        for _attempt in range(self.retries):
            try:
                response = self.opener(request, timeout=self.timeout_seconds)
                payload = json.loads(response.read().decode("utf-8", errors="replace"))
                observations = payload.get("observations") if isinstance(payload, dict) else None
                if not isinstance(observations, list):
                    raise RuntimeError("FRED API response did not contain an observations list")
                return observations, safe_url
            except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
        raise RuntimeError(f"FRED API request failed after {self.retries} attempt(s): {last_error}") from last_error

    def fetch_csv(self, series_id: str, *, start: str | None = None, end: str | None = None) -> tuple[str, str]:
        params = {"id": series_id.strip().upper()}
        if start:
            params["cosd"] = start
        if end:
            params["coed"] = end
        url = f"{FRED_CSV_URL}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": "IBKRMarketEventRecorder/0.14"})
        last_error: Exception | None = None
        for _attempt in range(self.retries):
            try:
                response = self.opener(request, timeout=self.timeout_seconds)
                raw = response.read().decode("utf-8", errors="replace")
                return raw, url
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
        raise RuntimeError(f"FRED CSV request failed after {self.retries} attempt(s): {last_error}") from last_error


class FredCollector:
    def __init__(self, database: Any, client: FredCsvClient) -> None:
        self.database = database
        self.client = client

    def sync(
        self,
        series_ids: tuple[str, ...],
        *,
        start: str | None = None,
        end: str | None = None,
        limit_per_series: int = 5000,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "ingestion_version": FRED_INGESTION_VERSION,
            "series_ids": [series.strip().upper() for series in series_ids],
            "stored": 0,
            "updated": 0,
            "errors": 0,
            "results": [],
        }
        for series_id in series_ids:
            item = {"series_id": series_id.strip().upper(), "observations": 0, "stored": 0, "updated": 0, "error": None}
            try:
                observations = self.observations_for_series(
                    series_id,
                    start=start,
                    end=end,
                    limit=limit_per_series,
                )
                item["observations"] = len(observations)
                for observation in observations:
                    _, created = self.database.upsert_fred_observation(observation)
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

    def observations_for_series(
        self,
        series_id: str,
        *,
        start: str | None = None,
        end: str | None = None,
        limit: int = 5000,
    ) -> list[FredObservation]:
        normalized = series_id.strip().upper()
        if self.client.api_key:
            observations, url = self.client.fetch_api_observations(
                normalized,
                start=start,
                end=end,
                limit=limit,
            )
            rows: list[FredObservation] = []
            for observation in observations:
                if not isinstance(observation, dict):
                    continue
                date_value = str(observation.get("date") or "").strip()
                raw_value = str(observation.get("value") or "").strip()
                if not date_value or not raw_value or raw_value == ".":
                    continue
                try:
                    value = float(raw_value)
                except ValueError:
                    continue
                rows.append(
                    FredObservation(
                        series_id=normalized,
                        observation_date=date_value,
                        value=value,
                        realtime_start=str(observation.get("realtime_start") or ""),
                        realtime_end=str(observation.get("realtime_end") or ""),
                        source_url=url,
                        source_hash=_sha256(f"{normalized}|{date_value}|{raw_value}"),
                    )
                )
            rows.sort(key=lambda item: item.observation_date, reverse=True)
            return rows[: max(0, int(limit))]

        raw, url = self.client.fetch_csv(normalized, start=start, end=end)
        rows: list[FredObservation] = []
        for row in csv.DictReader(io.StringIO(raw)):
            date_value = str(row.get("observation_date") or row.get("DATE") or "").strip()
            raw_value = str(row.get(normalized) or "").strip()
            if not date_value or not raw_value or raw_value == ".":
                continue
            try:
                value = float(raw_value)
            except ValueError:
                continue
            rows.append(
                FredObservation(
                    series_id=normalized,
                    observation_date=date_value,
                    value=value,
                    source_url=url,
                    source_hash=_sha256(f"{normalized}|{date_value}|{raw_value}"),
                )
            )
        rows.sort(key=lambda item: item.observation_date, reverse=True)
        return rows[: max(0, int(limit))]
