from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from .database import utc_iso
from .sec_edgar import SEC_COMPANY_TICKERS_URL

SEC_COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
FINANCIAL_FACTS_INGESTION_VERSION = "sec-companyfacts-v1"

DEFAULT_FINANCIAL_FACT_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "CostOfRevenue",
    "GrossProfit",
    "OperatingIncomeLoss",
    "NetIncomeLoss",
    "EarningsPerShareDiluted",
    "Assets",
    "Liabilities",
    "StockholdersEquity",
    "CashAndCashEquivalentsAtCarryingValue",
    "NetCashProvidedByUsedInOperatingActivities",
    "PaymentsToAcquirePropertyPlantAndEquipment",
)


@dataclass(frozen=True)
class FinancialStatementFact:
    symbol: str
    cik: str
    taxonomy: str
    tag: str
    label: str
    unit: str
    fiscal_year: int | None
    fiscal_period: str
    form: str
    filed_at: str
    start_date: str | None
    end_date: str
    frame: str
    value: float
    source_url: str
    accession_number: str
    source_hash: str
    ingestion_version: str = FINANCIAL_FACTS_INGESTION_VERSION


def _sha256_json(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class SecCompanyFactsClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 20.0,
        max_bytes: int = 8_000_000,
        min_interval_seconds: float = 0.2,
        opener: Callable[..., object] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not user_agent or "@" not in user_agent:
            raise ValueError("SEC_EDGAR_USER_AGENT must identify the requester and include a contact email address")
        self.user_agent = user_agent
        self.timeout_seconds = float(timeout_seconds)
        self.max_bytes = int(max_bytes)
        self.min_interval_seconds = float(min_interval_seconds)
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper
        self.clock = clock
        self._last_request_at: float | None = None
        self._ticker_cache: dict[str, int] | None = None

    def _json(self, url: str) -> dict[str, Any]:
        if self._last_request_at is not None:
            pause = self.min_interval_seconds - (self.clock() - self._last_request_at)
            if pause > 0:
                self.sleeper(pause)
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.user_agent,
                "Accept": "application/json,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            response = self.opener(request, timeout=self.timeout_seconds)
            self._last_request_at = self.clock()
            data = response.read(self.max_bytes + 1)
            if len(data) > self.max_bytes:
                raise ValueError(f"SEC companyfacts response exceeds configured limit ({self.max_bytes} bytes)")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"SEC companyfacts request failed: {exc}") from exc
        try:
            parsed = json.loads(data.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"SEC companyfacts returned invalid JSON for {url}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(f"SEC companyfacts returned a non-object JSON response for {url}")
        return parsed

    def ticker_cik(self, symbol: str) -> int:
        wanted = symbol.strip().upper()
        if self._ticker_cache is None:
            listing = self._json(SEC_COMPANY_TICKERS_URL)
            cache: dict[str, int] = {}
            for item in listing.values():
                if not isinstance(item, dict):
                    continue
                ticker = str(item.get("ticker") or "").upper()
                cik = item.get("cik_str")
                if ticker and cik is not None:
                    cache[ticker] = int(cik)
            self._ticker_cache = cache
        if wanted not in self._ticker_cache:
            raise LookupError(f"SEC ticker directory has no ticker-directory entry for {wanted}")
        return self._ticker_cache[wanted]

    def companyfacts(self, symbol: str) -> tuple[int, dict[str, Any], str]:
        cik = self.ticker_cik(symbol)
        url = SEC_COMPANYFACTS_URL.format(cik=cik)
        return cik, self._json(url), url


class SecFinancialFactsCollector:
    def __init__(self, database: Any, client: SecCompanyFactsClient) -> None:
        self.database = database
        self.client = client

    def sync_symbols(
        self,
        symbols: tuple[str, ...],
        *,
        tags: tuple[str, ...] = DEFAULT_FINANCIAL_FACT_TAGS,
        limit_per_symbol: int = 250,
    ) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "ingestion_version": FINANCIAL_FACTS_INGESTION_VERSION,
            "symbols": list(symbols),
            "stored": 0,
            "updated": 0,
            "errors": 0,
            "results": [],
        }
        for symbol in symbols:
            item = {"symbol": symbol.upper(), "facts": 0, "stored": 0, "updated": 0, "error": None}
            try:
                facts = self.facts_for_symbol(symbol, tags=tags, limit=limit_per_symbol)
                item["facts"] = len(facts)
                for fact in facts:
                    _, created = self.database.upsert_financial_statement_fact(fact)
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

    def facts_for_symbol(
        self,
        symbol: str,
        *,
        tags: tuple[str, ...] = DEFAULT_FINANCIAL_FACT_TAGS,
        limit: int = 250,
    ) -> list[FinancialStatementFact]:
        normalized_symbol = symbol.strip().upper()
        cik_int, payload, source_url = self.client.companyfacts(normalized_symbol)
        cik = str(cik_int).zfill(10)
        facts_root = payload.get("facts") if isinstance(payload.get("facts"), dict) else {}
        wanted_tags = {tag.strip() for tag in tags if tag.strip()}
        rows: list[FinancialStatementFact] = []
        for taxonomy, taxonomy_payload in facts_root.items():
            if not isinstance(taxonomy_payload, dict):
                continue
            for tag, fact_payload in taxonomy_payload.items():
                if wanted_tags and tag not in wanted_tags:
                    continue
                if not isinstance(fact_payload, dict):
                    continue
                label = str(fact_payload.get("label") or "")
                units = fact_payload.get("units") if isinstance(fact_payload.get("units"), dict) else {}
                for unit, observations in units.items():
                    if not isinstance(observations, list):
                        continue
                    for observation in observations:
                        if not isinstance(observation, dict):
                            continue
                        form = str(observation.get("form") or "")
                        if form not in {"10-K", "10-Q", "20-F", "40-F"}:
                            continue
                        value = observation.get("val")
                        end_date = str(observation.get("end") or "")
                        filed_at = str(observation.get("filed") or "")
                        accession = str(observation.get("accn") or "")
                        if value is None or not end_date or not accession:
                            continue
                        try:
                            value_float = float(value)
                        except (TypeError, ValueError):
                            continue
                        rows.append(
                            FinancialStatementFact(
                                symbol=normalized_symbol,
                                cik=cik,
                                taxonomy=str(taxonomy),
                                tag=str(tag),
                                label=label,
                                unit=str(unit),
                                fiscal_year=int(observation["fy"]) if observation.get("fy") is not None else None,
                                fiscal_period=str(observation.get("fp") or ""),
                                form=form,
                                filed_at=filed_at,
                                start_date=str(observation.get("start") or "") or None,
                                end_date=end_date,
                                frame=str(observation.get("frame") or ""),
                                value=value_float,
                                source_url=source_url,
                                accession_number=accession,
                                source_hash=_sha256_json(
                                    {
                                        "symbol": normalized_symbol,
                                        "taxonomy": taxonomy,
                                        "tag": tag,
                                        "unit": unit,
                                        "observation": observation,
                                    }
                                ),
                            )
                        )
        rows.sort(key=lambda fact: (fact.filed_at, fact.end_date, fact.tag), reverse=True)
        return rows[: max(0, int(limit))]
