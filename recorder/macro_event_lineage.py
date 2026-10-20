from __future__ import annotations

import json
import re
import sqlite3
import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .news_lineage_v2 import MACRO_EVENT_LINEAGE_VERSION, ensure_news_lineage_v2_schema

FOMC_CALENDAR_SOURCE_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
S04_FOMC_SEED_VERSION = "s04b-fomc-official-calendar-seed-v1"
ECB_CALENDAR_SOURCE_URL = "https://www.ecb.europa.eu/press/govcdec/mopo/html/index.en.html"
BOJ_CALENDAR_SOURCE_URL = "https://www.boj.or.jp/en/mopo/mpmsche_minu/index.htm"
S04P1_REGISTRY_VERSION = "s04p1-macro-semantic-registry-v1"
S04P1_ECB_SEED_VERSION = "s04p1-ecb-official-calendar-seed-v1"
S04P1_BOJ_SEED_VERSION = "s04p1-boj-official-calendar-seed-v1"
EASTERN = ZoneInfo("America/New_York")
EUROPE_BERLIN = ZoneInfo("Europe/Berlin")
ASIA_TOKYO = ZoneInfo("Asia/Tokyo")

NON_REACTION_EVENT_TYPES = {
    "rates_market_observation",
    "policy_rate_observation",
    "fx_market_observation",
    "energy_price_observation",
}
REACTION_EVENT_TYPES = {
    "macro_data_release",
    "energy_inventory_release",
    "central_bank_decision",
    "central_bank_statement",
    "central_bank_minutes",
    "central_bank_press_conference",
    "central_bank_projection",
    "central_bank_speech",
}
ENERGY_INVENTORY_KEYWORDS = (
    "inventory",
    "inventories",
    "stock",
    "stocks",
    "storage",
    "supply",
    "petroleum status",
)
ENERGY_PRICE_KEYWORDS = (
    "price",
    "wti",
    "brent",
    "gasoline price",
    "diesel price",
    "heating oil price",
    "natural gas price",
)
STRICT_REPLAY_VINTAGE_QUALITIES = {
    "fred_realtime_observation",
    "provider_realtime_observation",
    "alfred_vintage",
    "official_initial_release",
    "official_fomc_calendar",
}
EXACT_RELEASE_TIME_QUALITIES = {"official_exact", "provider_exact"}
DATE_ONLY_RELEASE_TIME_QUALITIES = {"official_date_only"}
OBSERVATION_ONLY_RELEASE_TIME_QUALITIES = {
    "observation_date_only",
    "backfilled_observation_date",
    "estimated",
    "unknown",
}

SCHEDULED_RELEASE_SERIES_ALLOWLIST = {
    "CPIAUCSL",
    "CPILFESL",
    "PPIACO",
    "PCEPI",
    "PCEPILFE",
    "RSAFS",
    "RSXFS",
    "PAYEMS",
    "UNRATE",
    "ICSA",
    "CES0500000003",
    "JTSJOL",
    "GDP",
    "GDPC1",
    "A191RL1Q225SBEA",
    "INDPRO",
    "HOUST",
    "PERMIT",
    "UMCSENT",
    "GACDISA066MSFRBNY",
    "GACDFSA066MSFRBPHI",
    "BACTSAMFRBDAL",
    "EUROSTAT_EA20_HICP_CP00",
    "EUROSTAT_EU27_UNRATE",
    "EUROSTAT_EA20_INDUSTRIAL_PRODUCTION",
    "ESTAT_JP_CPI",
    "ESTAT_JP_UNRATE",
    "ESTAT_JP_INDUSTRIAL_PRODUCTION",
}

REGIME_SERIES_EXACT = {
    "FEDFUNDS": ("policy_rate_observation", "policy_rate_observation"),
    "SOFR": ("money_market_rate_observation", "policy_rate_observation"),
    "EFFR": ("money_market_rate_observation", "policy_rate_observation"),
    "RRPONTSYD": ("liquidity_regime_observation", "liquidity_regime_observation"),
    "WALCL": ("liquidity_regime_observation", "liquidity_regime_observation"),
    "M2SL": ("liquidity_regime_observation", "liquidity_regime_observation"),
    "BUSLOANS": ("credit_regime_observation", "credit_regime_observation"),
    "TOTLL": ("credit_regime_observation", "credit_regime_observation"),
    "VIXCLS": ("volatility_regime_observation", "volatility_regime_observation"),
    "MORTGAGE30US": ("mortgage_rate_observation", "rates_market_observation"),
    "DTWEXBGS": ("fx_market_observation", "fx_market_observation"),
    "DCOILWTICO": ("energy_price_observation", "energy_price_observation"),
    "DCOILBRENTEU": ("energy_price_observation", "energy_price_observation"),
    "ECBDFR": ("policy_rate_observation", "policy_rate_observation"),
    "ECBMRRFR": ("policy_rate_observation", "policy_rate_observation"),
    "IUDSOIA": ("policy_rate_observation", "policy_rate_observation"),
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalise_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_") or "unknown"


def _hash_key(prefix: str, *parts: Any) -> str:
    payload = json.dumps([str(part or "").strip() for part in parts], ensure_ascii=False, separators=(",", ":"))
    return f"{prefix}:{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:32]}"


def _safe_date(value: str) -> str:
    return str(value or "").strip()[:10]


def _to_utc_iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _text_contains_any(text: str, needles: Iterable[str]) -> bool:
    lowered = text.lower()
    return any(needle in lowered for needle in needles)


@dataclass(frozen=True)
class MacroObservationInput:
    series_id: str = ""
    indicator_name: str = ""
    actual_value: float | None = None
    forecast_value: float | None = None
    prior_value: float | None = None
    revised_prior_value: float | None = None
    unit: str = ""
    period: str = ""
    frequency: str = ""
    seasonal_adjustment: str = ""
    surprise_raw: float | None = None
    surprise_pct: float | None = None
    surprise_zscore: float | None = None
    revision_delta: float | None = None
    vintage_date: str = ""
    vintage_quality: str = "unknown"
    data_source: str = ""
    data_version: str = ""
    raw_json: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroEventReleaseInput:
    source_system: str
    release_name: str
    scheduled_at_utc: str
    country: str = ""
    region: str = ""
    currency: str = ""
    event_type: str = "macro_data_release"
    released_at_utc: str = ""
    available_at_utc: str = ""
    model_visible_at_utc: str = ""
    release_time_quality: str = "unknown"
    importance: str = "unknown"
    expected_importance: str = "unknown"
    source_calendar: str = ""
    calendar_version: str = MACRO_EVENT_LINEAGE_VERSION
    source_url: str = ""
    raw_json: dict[str, Any] = field(default_factory=dict)
    observations: tuple[MacroObservationInput, ...] = ()
    central_bank: str = ""
    central_bank_payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MacroSemanticPolicy:
    registry_key: str
    semantic_type: str
    event_type: str
    event_cluster_eligible: int
    reaction_trigger_eligible: int
    regime_feature_eligible: int
    intraday_label_eligible: int
    release_time_quality: str
    release_time_policy: str
    event_eligibility_reason: str


def _normalise_release_time_quality(value: str) -> str:
    quality = str(value or "").strip().lower()
    aliases = {
        "official_fomc_calendar_1400_et": "official_exact",
        "official_ecb_decision_1415_local": "official_exact",
        "official_ecb_press_conference_1445_local": "official_exact",
        "official_boj_0850_jst": "official_exact",
        "backfilled_observation_date": "observation_date_only",
    }
    return aliases.get(quality, quality or "unknown")


def _series_id_for_payload(payload: MacroEventReleaseInput) -> str:
    obs = _first_observation(payload)
    return str(obs.series_id or "").strip().upper() if obs else ""


def _registry_key(source_system: str, series_id: str) -> str:
    return _hash_key("macro_registry", S04P1_REGISTRY_VERSION, source_system or "*", series_id)


def _series_semantic_definition(series_id: str, category: str = "") -> tuple[str, str, bool, str]:
    sid = str(series_id or "").strip().upper()
    normalized_category = str(category or "").strip().lower()
    if sid in REGIME_SERIES_EXACT:
        semantic_type, event_type = REGIME_SERIES_EXACT[sid]
        return semantic_type, event_type, False, "explicit_regime_series"
    if sid.startswith("DGS") or sid.startswith("IRLTLT"):
        return "sovereign_yield_observation", "rates_market_observation", False, "yield_series_regime_feature"
    if sid.startswith("T10Y"):
        return "yield_curve_observation", "rates_market_observation", False, "yield_curve_regime_feature"
    if sid.startswith("BAML"):
        return "credit_spread_observation", "credit_spread_observation", False, "credit_spread_regime_feature"
    if sid.startswith("DEX"):
        return "fx_market_observation", "fx_market_observation", False, "fx_series_regime_feature"
    if sid in SCHEDULED_RELEASE_SERIES_ALLOWLIST:
        return "scheduled_macro_release_series", "macro_data_release", True, "explicit_scheduled_release_allowlist"
    if normalized_category in {
        "policy_rates",
        "rates_curve",
        "housing_credit",
        "credit_risk",
        "volatility",
        "liquidity",
        "credit",
        "fx",
        "fx_dollar",
        "commodities_energy",
    }:
        return "market_regime_observation", "market_regime_observation", False, "category_regime_feature"
    return "unapproved_macro_observation", "macro_data_observation", False, "not_in_reaction_release_allowlist"


def classify_macro_payload(payload: MacroEventReleaseInput) -> MacroSemanticPolicy:
    quality = _normalise_release_time_quality(payload.release_time_quality)
    series_id = _series_id_for_payload(payload)
    source_system = str(payload.source_system or "").strip().lower()
    if payload.central_bank or str(payload.event_type or "").startswith("central_bank_"):
        event_type = str(payload.event_type or "central_bank_decision").strip()
        exact = quality in EXACT_RELEASE_TIME_QUALITIES
        date_only = quality in DATE_ONLY_RELEASE_TIME_QUALITIES
        allowed = exact or date_only
        return MacroSemanticPolicy(
            registry_key=_registry_key(source_system, f"CENTRAL_BANK:{payload.central_bank or source_system}"),
            semantic_type="scheduled_central_bank_event",
            event_type=event_type,
            event_cluster_eligible=1 if allowed else 0,
            reaction_trigger_eligible=1 if allowed else 0,
            regime_feature_eligible=1,
            intraday_label_eligible=1 if exact else 0,
            release_time_quality=quality,
            release_time_policy="exact_intraday" if exact else ("date_only_daily" if date_only else "not_reaction_eligible"),
            event_eligibility_reason=(
                "central_bank_official_exact_release"
                if exact
                else ("central_bank_official_date_only_release" if date_only else "central_bank_release_time_not_approved")
            ),
        )

    metadata = payload.raw_json.get("metadata") if isinstance(payload.raw_json, dict) else {}
    category = str((metadata or {}).get("category") or "") if isinstance(metadata, dict) else ""
    semantic_type, event_type, allowlisted_release, reason = _series_semantic_definition(series_id, category)
    if str(payload.event_type or "") == "energy_inventory_or_price_observation" and _text_contains_any(
        _payload_observation_text(payload), ENERGY_INVENTORY_KEYWORDS
    ) and series_id not in {"DCOILWTICO", "DCOILBRENTEU"}:
        semantic_type = "scheduled_energy_inventory_release"
        event_type = "energy_inventory_release"
        allowlisted_release = True
        reason = "explicit_energy_inventory_release"

    exact = quality in EXACT_RELEASE_TIME_QUALITIES
    date_only = quality in DATE_ONLY_RELEASE_TIME_QUALITIES
    reaction_eligible = bool(allowlisted_release and (exact or date_only))
    return MacroSemanticPolicy(
        registry_key=_registry_key("*", series_id or f"UNREGISTERED:{source_system}"),
        semantic_type=semantic_type,
        event_type=event_type,
        event_cluster_eligible=1 if reaction_eligible else 0,
        reaction_trigger_eligible=1 if reaction_eligible else 0,
        regime_feature_eligible=1,
        intraday_label_eligible=1 if reaction_eligible and exact else 0,
        release_time_quality=quality,
        release_time_policy=(
            "exact_intraday"
            if reaction_eligible and exact
            else ("date_only_daily" if reaction_eligible and date_only else "regime_feature_only")
        ),
        event_eligibility_reason=(
            f"{reason}:official_exact"
            if reaction_eligible and exact
            else (f"{reason}:official_date_only" if reaction_eligible and date_only else f"{reason}:no_approved_release_time")
        ),
    )


def _macro_event_class(event_type: str, release_name: str) -> str:
    text = f"{event_type} {release_name}".lower()
    if any(token in text for token in ("cpi", "pce", "inflation", "ppi")):
        return "macro_event.inflation"
    if any(token in text for token in ("payroll", "nfp", "jobless", "employment", "adp", "jolts", "wage", "unemployment")):
        return "macro_event.labor"
    if any(token in text for token in ("eia", "crude", "oil", "gasoline", "inventory", "petroleum")):
        return "macro_event.energy_inventory"
    if any(token in text for token in ("ism", "pmi", "manufacturing", "services", "industrial production", "gdp", "retail sales")):
        return "macro_event.growth_survey"
    if any(token in text for token in ("treasury", "yield", "curve", "sofr", "fed funds", "effective federal funds")):
        return "macro_event.rates"
    if any(token in text for token in ("fomc", "central_bank", "minutes", "speech", "boj", "ecb", "policy rate", "rate decision")):
        return "macro_event.central_bank"
    return "macro_event.release"


def _cluster_key(payload: MacroEventReleaseInput, event_type: str) -> str:
    obs = _first_observation(payload)
    if obs and obs.series_id:
        return ":".join(
            [
                "macro",
                _normalise_key(payload.source_system),
                _normalise_key(event_type),
                _normalise_key(obs.series_id),
                _normalise_key(obs.period),
            ]
        )
    return ":".join(
        [
            "macro",
            _normalise_key(payload.source_system),
            _normalise_key(event_type),
            _normalise_key(payload.country),
            _normalise_key(payload.release_name),
            _normalise_key(payload.scheduled_at_utc),
        ]
    )


def _coalesce_time(*values: str) -> str:
    for value in values:
        if value:
            return value
    return _utc_now_iso()


def _payload_observation_text(payload: MacroEventReleaseInput) -> str:
    parts = [payload.release_name, payload.event_type, payload.source_system]
    for obs in payload.observations:
        parts.extend([obs.series_id, obs.indicator_name, obs.period])
    return " ".join(str(part or "") for part in parts)


def _canonical_event_type(payload: MacroEventReleaseInput) -> str:
    return classify_macro_payload(payload).event_type


def _eligibility_for_event_type(event_type: str) -> dict[str, Any]:
    if str(event_type or "").startswith("central_bank_"):
        return {
            "event_cluster_eligible": 1,
            "reaction_trigger_eligible": 1,
            "regime_feature_eligible": 1,
            "event_eligibility_reason": "legacy_central_bank_event_type_only",
        }
    return {
        "event_cluster_eligible": 0,
        "reaction_trigger_eligible": 0,
        "regime_feature_eligible": 1,
        "event_eligibility_reason": "event_type_only_classification_forbidden_use_registry",
    }


def _first_observation(payload: MacroEventReleaseInput) -> MacroObservationInput | None:
    return payload.observations[0] if payload.observations else None


def _macro_event_key(payload: MacroEventReleaseInput, event_type: str) -> str:
    obs = _first_observation(payload)
    if obs and obs.series_id:
        return _hash_key(
            "macro_event",
            payload.source_system,
            event_type,
            obs.series_id,
            obs.period,
        )
    series_id = payload.central_bank or payload.release_name
    period = _safe_date(payload.scheduled_at_utc)
    return _hash_key(
        "macro_event",
        payload.source_system,
        event_type,
        series_id,
        payload.country,
        period,
    )


def _macro_calendar_key(payload: MacroEventReleaseInput, event_type: str) -> str:
    obs = _first_observation(payload)
    if obs and obs.series_id:
        return _hash_key(
            "macro_calendar",
            payload.source_system,
            event_type,
            obs.series_id,
            obs.period,
        )
    period = _safe_date(payload.scheduled_at_utc)
    return _hash_key(
        "macro_calendar",
        payload.source_system,
        event_type,
        payload.release_name,
        payload.country,
        period,
    )


def _macro_observation_key(macro_event_key: str, obs: MacroObservationInput) -> str:
    return _hash_key(
        "macro_observation",
        macro_event_key,
        obs.series_id,
        obs.indicator_name,
        obs.period,
        obs.vintage_date,
        obs.data_version,
    )


def _macro_cluster_link_key(macro_event_id: int, event_cluster_id: int, link_type: str) -> str:
    return _hash_key("macro_cluster_link", macro_event_id, event_cluster_id, link_type)


def _surprise_basis(obs: MacroObservationInput) -> tuple[str, int]:
    if obs.forecast_value is not None:
        return "consensus", 1
    if obs.revised_prior_value is not None and obs.revision_delta is not None:
        return "revision_delta", 0
    if obs.prior_value is not None and obs.surprise_raw is not None:
        return "prior_delta", 0
    if obs.surprise_zscore is not None:
        return "historical_zscore", 0
    if obs.surprise_raw is not None:
        return "level_change", 0
    return "unknown", 0


def _strict_replay_eligible(obs: MacroObservationInput, *, available: str, released: str, model_visible: str) -> int:
    if obs.vintage_quality not in STRICT_REPLAY_VINTAGE_QUALITIES:
        return 0
    if not available or not released or not model_visible:
        return 0
    if available < released:
        return 0
    return 1


def persist_macro_series_registry(conn: sqlite3.Connection) -> int:
    from .macro_universe import FRED_GLOBAL_CORE_SERIES

    now = _utc_now_iso()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in FRED_GLOBAL_CORE_SERIES:
        series_id = str(item.series_id or "").strip().upper()
        if not series_id or series_id in seen:
            continue
        seen.add(series_id)
        semantic_type, event_type, allowlisted, reason = _series_semantic_definition(series_id, str(item.category or ""))
        expected_frequency = ""
        category = str(item.category or "").lower()
        if series_id.startswith(("DGS", "DEX")) or series_id in {
            "SOFR",
            "EFFR",
            "VIXCLS",
            "DTWEXBGS",
            "DCOILWTICO",
            "DCOILBRENTEU",
            "BAMLC0A0CM",
            "BAMLH0A0HYM2",
            "RRPONTSYD",
        }:
            expected_frequency = "daily"
        elif "quarter" in category or series_id in {"GDP", "GDPC1", "A191RL1Q225SBEA"}:
            expected_frequency = "quarterly"
        elif series_id in {"ICSA", "IC4WSA", "CCSA", "WALCL", "MORTGAGE30US"}:
            expected_frequency = "weekly"
        else:
            expected_frequency = "monthly_or_provider_defined"
        rows.append(
            {
                "registry_key": _registry_key("*", series_id),
                "source_system": "*",
                "series_id": series_id,
                "release_name": str(item.label or ""),
                "semantic_type": semantic_type,
                "event_type": event_type,
                "event_cluster_eligible": 1 if allowlisted else 0,
                "reaction_trigger_eligible": 1 if allowlisted else 0,
                "regime_feature_eligible": 1,
                "intraday_label_eligible": 1 if allowlisted else 0,
                "release_calendar_source": str(item.source_family or ""),
                "expected_frequency": expected_frequency,
                "release_time_quality_required": (
                    "official_exact_or_provider_exact_or_official_date_only" if allowlisted else "not_applicable_regime_feature"
                ),
                "notes": reason,
            }
        )

    for source_system, central_bank, required_types in (
        ("federal_reserve", "Fed", "decision,minutes,projection"),
        ("ecb", "ECB", "decision,statement,press_conference"),
        ("boj", "BOJ", "decision,statement,minutes_or_summary"),
    ):
        series_id = f"CENTRAL_BANK:{central_bank}"
        rows.append(
            {
                "registry_key": _registry_key(source_system, series_id),
                "source_system": source_system,
                "series_id": series_id,
                "release_name": f"{central_bank} official monetary-policy calendar",
                "semantic_type": "scheduled_central_bank_event",
                "event_type": "central_bank_event",
                "event_cluster_eligible": 1,
                "reaction_trigger_eligible": 1,
                "regime_feature_eligible": 1,
                "intraday_label_eligible": 1,
                "release_calendar_source": source_system,
                "expected_frequency": "meeting_calendar",
                "release_time_quality_required": "official_exact_or_official_date_only",
                "notes": f"global_macro_v1 required coverage: {required_types}",
            }
        )

    for row in rows:
        conn.execute(
            """INSERT INTO macro_series_registry(
                   registry_key,source_system,series_id,release_name,semantic_type,event_type,
                   event_cluster_eligible,reaction_trigger_eligible,regime_feature_eligible,
                   intraday_label_eligible,release_calendar_source,expected_frequency,
                   release_time_quality_required,registry_version,build_scope,ibkr_market_data_policy,
                   valid_from,valid_to,notes,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(registry_key) DO UPDATE SET
                   release_name=excluded.release_name,
                   semantic_type=excluded.semantic_type,
                   event_type=excluded.event_type,
                   event_cluster_eligible=excluded.event_cluster_eligible,
                   reaction_trigger_eligible=excluded.reaction_trigger_eligible,
                   regime_feature_eligible=excluded.regime_feature_eligible,
                   intraday_label_eligible=excluded.intraday_label_eligible,
                   release_calendar_source=excluded.release_calendar_source,
                   expected_frequency=excluded.expected_frequency,
                   release_time_quality_required=excluded.release_time_quality_required,
                   build_scope=excluded.build_scope,
                   ibkr_market_data_policy=excluded.ibkr_market_data_policy,
                   notes=excluded.notes,
                   updated_at=excluded.updated_at""",
            (
                row["registry_key"],
                row["source_system"],
                row["series_id"],
                row["release_name"],
                row["semantic_type"],
                row["event_type"],
                row["event_cluster_eligible"],
                row["reaction_trigger_eligible"],
                row["regime_feature_eligible"],
                row["intraday_label_eligible"],
                row["release_calendar_source"],
                row["expected_frequency"],
                row["release_time_quality_required"],
                S04P1_REGISTRY_VERSION,
                "global_macro_v1",
                "ibkr_read_only_market_data_after_security_master_resolution",
                "2025-01-01",
                "",
                row["notes"],
                now,
                now,
            ),
        )
    return len(rows)


def ensure_macro_event_lineage_schema(conn: sqlite3.Connection) -> None:
    if conn.execute(
        "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name='_macro_event_lineage_schema_ensured'"
    ).fetchone():
        return
    ensure_news_lineage_v2_schema(conn)
    persist_macro_series_registry(conn)
    conn.execute("CREATE TEMP TABLE IF NOT EXISTS _macro_event_lineage_schema_ensured (ensured INTEGER NOT NULL)")


def upsert_macro_event_release(
    conn: sqlite3.Connection,
    payload: MacroEventReleaseInput,
) -> int:
    """Store a first-class macro release and link it into the event-cluster graph.

    Scheduled economic releases are intentionally not stored as fake
    `raw_news_articles`.  They have their own raw macro lineage and become
    first-class `news_event_clusters` so BZ/DJNL/Marketaux articles can later
    link to the same market-moving event.
    """

    ensure_macro_event_lineage_schema(conn)
    now = _utc_now_iso()
    policy = classify_macro_payload(payload)
    event_type = policy.event_type
    eligibility = {
        "event_cluster_eligible": policy.event_cluster_eligible,
        "reaction_trigger_eligible": policy.reaction_trigger_eligible,
        "regime_feature_eligible": policy.regime_feature_eligible,
        "intraday_label_eligible": policy.intraday_label_eligible,
        "event_eligibility_reason": policy.event_eligibility_reason,
    }
    macro_event_key = _macro_event_key(payload, event_type)
    macro_calendar_key = _macro_calendar_key(payload, event_type)
    scheduled = payload.scheduled_at_utc
    released = _coalesce_time(payload.released_at_utc, scheduled)
    available = _coalesce_time(payload.available_at_utc, released, scheduled)
    model_visible = _coalesce_time(payload.model_visible_at_utc, available)
    raw_json = json.dumps(payload.raw_json or {}, ensure_ascii=False, sort_keys=True)

    calendar_row = conn.execute(
        "SELECT calendar_event_id FROM macro_release_calendar WHERE macro_calendar_key=?",
        (macro_calendar_key,),
    ).fetchone()
    if calendar_row is None:
        calendar_row = conn.execute(
            """SELECT calendar_event_id
                 FROM macro_release_calendar
                WHERE source_system=? AND release_name=? AND country=? AND scheduled_at_utc=? AND calendar_version=?
                ORDER BY calendar_event_id DESC
                LIMIT 1""",
            (payload.source_system, payload.release_name, payload.country, scheduled, payload.calendar_version),
        ).fetchone()
    if calendar_row is None:
        conn.execute(
            """INSERT INTO macro_release_calendar(
                   macro_calendar_key,source_system,release_name,country,region,currency,event_type,scheduled_at_utc,
                   expected_importance,source_calendar,calendar_version,raw_json,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                macro_calendar_key,
                payload.source_system,
                payload.release_name,
                payload.country,
                payload.region,
                payload.currency,
                event_type,
                scheduled,
                payload.expected_importance,
                payload.source_calendar,
                payload.calendar_version,
                raw_json,
                now,
            ),
        )
        calendar_event_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    else:
        calendar_event_id = int(calendar_row["calendar_event_id"])
        conn.execute(
            """UPDATE macro_release_calendar
                  SET macro_calendar_key=?,
                      region=?,
                      currency=?,
                      event_type=?,
                      expected_importance=?,
                      source_calendar=?,
                      calendar_version=?,
                      raw_json=?
                WHERE calendar_event_id=?""",
            (
                macro_calendar_key,
                payload.region,
                payload.currency,
                event_type,
                payload.expected_importance,
                payload.source_calendar,
                payload.calendar_version,
                raw_json,
                calendar_event_id,
            ),
        )

    event_cluster_id: int | None = None
    if eligibility["reaction_trigger_eligible"]:
        cluster_key = _cluster_key(payload, event_type)
        event_class = _macro_event_class(event_type, payload.release_name)
        primary_event_name = f"{payload.release_name} {scheduled}".strip()
        conn.execute(
            """INSERT OR IGNORE INTO news_event_clusters(
                   cluster_key,cluster_type,primary_headline,primary_event_name,
                   first_seen_at_utc,last_seen_at_utc,canonical_entities_json,event_class,
                   provider_count,article_count,macro_release_count,cluster_status,clusterer_version,
                   country,currency,reaction_trigger_eligible,cluster_origin,created_at,updated_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cluster_key,
                "macro_release" if not payload.central_bank else "central_bank_event",
                primary_event_name,
                primary_event_name,
                available,
                available,
                "[]",
                event_class,
                0,
                0,
                1,
                "candidate",
                MACRO_EVENT_LINEAGE_VERSION,
                payload.country,
                payload.currency,
                1,
                "macro_event",
                now,
                now,
            ),
        )
        cluster = conn.execute(
            "SELECT event_cluster_id FROM news_event_clusters WHERE cluster_key=?",
            (cluster_key,),
        ).fetchone()
        event_cluster_id = int(cluster["event_cluster_id"])
        conn.execute(
            """UPDATE news_event_clusters
                  SET primary_event_name=?,
                      first_seen_at_utc=CASE WHEN first_seen_at_utc='' OR first_seen_at_utc>? THEN ? ELSE first_seen_at_utc END,
                      last_seen_at_utc=CASE WHEN last_seen_at_utc<? THEN ? ELSE last_seen_at_utc END,
                      event_class=?,
                      cluster_type=?,
                      country=?,
                      currency=?,
                      reaction_trigger_eligible=1,
                      cluster_origin=CASE WHEN cluster_origin='' THEN 'macro_event' ELSE cluster_origin END,
                      macro_release_count=MAX(macro_release_count,1),
                      updated_at=?
                WHERE event_cluster_id=?""",
            (
                primary_event_name,
                available,
                available,
                available,
                available,
                event_class,
                "macro_release" if not payload.central_bank else "central_bank_event",
                payload.country,
                payload.currency,
                now,
                event_cluster_id,
            ),
        )

    release_row = conn.execute(
        "SELECT macro_event_id FROM macro_event_releases WHERE macro_event_key=?",
        (macro_event_key,),
    ).fetchone()
    if release_row is None:
        release_row = conn.execute(
            """SELECT macro_event_id
                 FROM macro_event_releases
                WHERE source_system=? AND release_name=? AND country=? AND scheduled_at_utc=? AND event_type=?
                ORDER BY macro_event_id DESC
                LIMIT 1""",
            (payload.source_system, payload.release_name, payload.country, scheduled, event_type),
        ).fetchone()
    if release_row is None:
        conn.execute(
            """INSERT INTO macro_event_releases(
                   calendar_event_id,event_cluster_id,macro_event_key,source_system,release_name,country,region,currency,
                   event_type,scheduled_at_utc,released_at_utc,available_at_utc,model_visible_at_utc,
                   release_time_quality,importance,source_url,event_cluster_eligible,reaction_trigger_eligible,
                   regime_feature_eligible,intraday_label_eligible,semantic_type,registry_key,registry_version,
                   release_time_policy,event_eligibility_reason,raw_json,lineage_version,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                calendar_event_id,
                event_cluster_id,
                macro_event_key,
                payload.source_system,
                payload.release_name,
                payload.country,
                payload.region,
                payload.currency,
                event_type,
                scheduled,
                released,
                available,
                model_visible,
                policy.release_time_quality,
                payload.importance,
                payload.source_url,
                eligibility["event_cluster_eligible"],
                eligibility["reaction_trigger_eligible"],
                eligibility["regime_feature_eligible"],
                eligibility["intraday_label_eligible"],
                policy.semantic_type,
                policy.registry_key,
                S04P1_REGISTRY_VERSION,
                policy.release_time_policy,
                eligibility["event_eligibility_reason"],
                raw_json,
                MACRO_EVENT_LINEAGE_VERSION,
                now,
            ),
        )
        macro_event_id = int(conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
    else:
        macro_event_id = int(release_row["macro_event_id"])
        conn.execute(
            """UPDATE macro_event_releases
                  SET calendar_event_id=?,
                      event_cluster_id=?,
                      macro_event_key=?,
                      release_name=?,
                      region=?,
                      currency=?,
                      event_type=?,
                      released_at_utc=?,
                      available_at_utc=?,
                      model_visible_at_utc=?,
                      release_time_quality=?,
                      importance=?,
                      source_url=?,
                      event_cluster_eligible=?,
                      reaction_trigger_eligible=?,
                      regime_feature_eligible=?,
                      intraday_label_eligible=?,
                      semantic_type=?,
                      registry_key=?,
                      registry_version=?,
                      release_time_policy=?,
                      event_eligibility_reason=?,
                      raw_json=?,
                      lineage_version=?
                WHERE macro_event_id=?""",
            (
                calendar_event_id,
                event_cluster_id,
                macro_event_key,
                payload.release_name,
                payload.region,
                payload.currency,
                event_type,
                released,
                available,
                model_visible,
                policy.release_time_quality,
                payload.importance,
                payload.source_url,
                eligibility["event_cluster_eligible"],
                eligibility["reaction_trigger_eligible"],
                eligibility["regime_feature_eligible"],
                eligibility["intraday_label_eligible"],
                policy.semantic_type,
                policy.registry_key,
                S04P1_REGISTRY_VERSION,
                policy.release_time_policy,
                eligibility["event_eligibility_reason"],
                raw_json,
                MACRO_EVENT_LINEAGE_VERSION,
                macro_event_id,
            ),
        )

    for obs in payload.observations:
        observation_key = _macro_observation_key(macro_event_key, obs)
        surprise_basis, is_consensus_surprise = _surprise_basis(obs)
        strict_replay = _strict_replay_eligible(obs, available=available, released=released, model_visible=model_visible)
        existing_obs = conn.execute(
            "SELECT macro_observation_id FROM macro_observations WHERE macro_observation_key=?",
            (observation_key,),
        ).fetchone()
        if existing_obs is None:
            existing_obs = conn.execute(
                """SELECT macro_observation_id
                     FROM macro_observations
                    WHERE macro_event_id=? AND series_id=? AND indicator_name=? AND period=? AND vintage_date=? AND data_version=?
                    ORDER BY macro_observation_id DESC
                    LIMIT 1""",
                (macro_event_id, obs.series_id, obs.indicator_name, obs.period, obs.vintage_date, obs.data_version),
            ).fetchone()
        values = (
            macro_event_id,
            observation_key,
            obs.series_id,
            obs.indicator_name,
            obs.actual_value,
            obs.forecast_value,
            obs.prior_value,
            obs.revised_prior_value,
            obs.unit,
            obs.period,
            obs.frequency,
            obs.seasonal_adjustment,
            obs.surprise_raw,
            obs.surprise_pct,
            obs.surprise_zscore,
            obs.revision_delta,
            obs.vintage_date,
            obs.vintage_quality,
            obs.data_source,
            obs.data_version,
            strict_replay,
            surprise_basis,
            is_consensus_surprise,
            json.dumps(obs.raw_json or {}, ensure_ascii=False, sort_keys=True),
        )
        if existing_obs is None:
            conn.execute(
                """INSERT INTO macro_observations(
                       macro_event_id,macro_observation_key,series_id,indicator_name,actual_value,forecast_value,prior_value,
                       revised_prior_value,unit,period,frequency,seasonal_adjustment,surprise_raw,
                       surprise_pct,surprise_zscore,revision_delta,vintage_date,vintage_quality,
                       data_source,data_version,strict_replay_eligible,surprise_basis,is_consensus_surprise,raw_json,created_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, now),
            )
        else:
            conn.execute(
                """UPDATE macro_observations
                      SET macro_event_id=?,
                          macro_observation_key=?,
                          series_id=?,
                          indicator_name=?,
                          actual_value=?,
                          forecast_value=?,
                          prior_value=?,
                          revised_prior_value=?,
                          unit=?,
                          period=?,
                          frequency=?,
                          seasonal_adjustment=?,
                          surprise_raw=?,
                          surprise_pct=?,
                          surprise_zscore=?,
                          revision_delta=?,
                          vintage_date=?,
                          vintage_quality=?,
                          data_source=?,
                          data_version=?,
                          strict_replay_eligible=?,
                          surprise_basis=?,
                          is_consensus_surprise=?,
                          raw_json=?
                    WHERE macro_observation_id=?""",
                (*values, int(existing_obs["macro_observation_id"])),
            )

    if payload.central_bank:
        cb = payload.central_bank_payload or {}
        conn.execute(
            """INSERT OR REPLACE INTO central_bank_events(
                   macro_event_id,central_bank,decision_type,policy_rate_actual,policy_rate_expected,
                   policy_rate_prior,rate_surprise_bp,statement_text,minutes_text,projection_json,
                   speaker,hawkish_dovish_score,text_classifier_version,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                macro_event_id,
                payload.central_bank,
                str(cb.get("decision_type") or event_type),
                cb.get("policy_rate_actual"),
                cb.get("policy_rate_expected"),
                cb.get("policy_rate_prior"),
                cb.get("rate_surprise_bp"),
                cb.get("statement_text"),
                cb.get("minutes_text"),
                json.dumps(cb.get("projection_json") or {}, ensure_ascii=False, sort_keys=True),
                str(cb.get("speaker") or ""),
                cb.get("hawkish_dovish_score"),
                str(cb.get("text_classifier_version") or S04_FOMC_SEED_VERSION),
                now,
            ),
        )

    if event_cluster_id is not None and eligibility["reaction_trigger_eligible"]:
        link_type = "primary_scheduled_event"
        link_key = _macro_cluster_link_key(macro_event_id, event_cluster_id, link_type)
        link_row = conn.execute(
            "SELECT 1 FROM macro_event_cluster_links WHERE macro_cluster_link_key=?",
            (link_key,),
        ).fetchone()
        if link_row is None:
            link_row = conn.execute(
                """SELECT 1 FROM macro_event_cluster_links
                    WHERE macro_event_id=? AND event_cluster_id=? AND link_type=?""",
                (macro_event_id, event_cluster_id, link_type),
            ).fetchone()
        if link_row is None:
            conn.execute(
                """INSERT INTO macro_event_cluster_links(
                       macro_cluster_link_key,macro_event_id,event_cluster_id,link_type,confidence,link_status,link_reason,created_at
                   ) VALUES (?,?,?,?,?,?,?,?)""",
                (link_key, macro_event_id, event_cluster_id, link_type, 1.0, "active", "reaction trigger macro event", now),
            )
        else:
            conn.execute(
                """UPDATE macro_event_cluster_links
                      SET macro_cluster_link_key=?,
                          confidence=1.0,
                          link_status='active',
                          link_reason='reaction trigger macro event'
                    WHERE macro_event_id=? AND event_cluster_id=? AND link_type=?""",
                (link_key, macro_event_id, event_cluster_id, link_type),
            )
    else:
        conn.execute(
            """UPDATE macro_event_cluster_links
                  SET link_status='disabled_regime_feature_only',
                      link_reason='macro observation is regime feature, not reaction trigger'
                WHERE macro_event_id=?""",
            (macro_event_id,),
        )

    return macro_event_id


def upsert_macro_event_releases(
    conn: sqlite3.Connection,
    payloads: Iterable[MacroEventReleaseInput],
) -> list[int]:
    return [upsert_macro_event_release(conn, payload) for payload in payloads]


def _region_to_country(region: str) -> str:
    normalized = str(region or "").strip().lower()
    mapping = {
        "us": "US",
        "united states": "US",
        "global": "GLOBAL",
        "europe": "EU",
        "euro area": "EU",
        "japan": "JP",
        "korea": "KR",
        "china": "CN",
        "uk": "GB",
        "united kingdom": "GB",
    }
    return mapping.get(normalized, str(region or "").upper())


def _country_to_currency(country: str) -> str:
    mapping = {
        "US": "USD",
        "GLOBAL": "USD",
        "EU": "EUR",
        "JP": "JPY",
        "KR": "KRW",
        "CN": "CNY",
        "GB": "GBP",
    }
    return mapping.get(str(country or "").upper(), "")


def _category_to_event_type(category: str) -> str:
    text = str(category or "").lower()
    if "policy" in text:
        return "policy_rate_observation"
    if "rates" in text or "yield" in text or "curve" in text:
        return "rates_market_observation"
    if "inflation" in text:
        return "macro_data_release"
    if "labor" in text or "employment" in text:
        return "macro_data_release"
    if "commod" in text or "energy" in text or "oil" in text:
        return "energy_inventory_or_price_observation"
    if "manufacturing" in text or "survey" in text or "growth" in text or "consumption" in text:
        return "macro_data_release"
    if "fx" in text:
        return "fx_market_observation"
    return "macro_data_release"


def _date_to_conservative_available_at(observation_date: str) -> str:
    date_text = str(observation_date or "").strip()[:10]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
        return f"{date_text}T23:59:59+00:00"
    return _utc_now_iso()


def _vintage_quality_for_row(row: sqlite3.Row, *, provider: str) -> str:
    realtime_start = str(row["realtime_start"] or "") if "realtime_start" in row.keys() else ""
    realtime_end = str(row["realtime_end"] or "") if "realtime_end" in row.keys() else ""
    if provider == "fred" and realtime_start and realtime_end:
        return "fred_realtime_observation"
    if provider == "fred":
        return "revised_latest_only"
    if realtime_start or realtime_end:
        return "provider_realtime_observation"
    return "provider_observation_date_only"


def _prior_for_series(conn: sqlite3.Connection, table: str, key_column: str, key_value: str, observation_date: str) -> float | None:
    if table == "fred_observations":
        row = conn.execute(
            """SELECT value
                 FROM fred_observations
                WHERE series_id=? AND observation_date<?
                ORDER BY observation_date DESC,id DESC
                LIMIT 1""",
            (key_value, observation_date),
        ).fetchone()
    else:
        row = conn.execute(
            f"""SELECT value
                  FROM {table}
                 WHERE {key_column}=? AND observation_date<?
                 ORDER BY observation_date DESC,provider_priority ASC,id DESC
                 LIMIT 1""",
            (key_value, observation_date),
        ).fetchone()
    return float(row["value"]) if row and row["value"] is not None else None


def _existing_macro_event_id(conn: sqlite3.Connection, payload: MacroEventReleaseInput) -> int | None:
    event_type = _canonical_event_type(payload)
    macro_event_key = _macro_event_key(payload, event_type)
    row = conn.execute(
        "SELECT macro_event_id FROM macro_event_releases WHERE macro_event_key=? AND macro_event_key<>''",
        (macro_event_key,),
    ).fetchone()
    return int(row["macro_event_id"]) if row else None


def _macro_identity_payload_from_fred_row(row: sqlite3.Row, metadata: dict[str, Any]) -> MacroEventReleaseInput:
    series_id = str(row["series_id"] or "").upper()
    meta = metadata.get(series_id)
    label = str(getattr(meta, "label", "") or series_id)
    region = str(getattr(meta, "region", "") or "")
    category = str(getattr(meta, "category", "") or "")
    country = _region_to_country(region)
    available_at = _date_to_conservative_available_at(str(row["observation_date"] or ""))
    observation = MacroObservationInput(
        series_id=series_id,
        indicator_name=label,
        period=str(row["observation_date"] or ""),
        vintage_date=str(row["realtime_start"] or row["observation_date"] or ""),
        vintage_quality=_vintage_quality_for_row(row, provider="fred"),
        data_source="fred",
        data_version=str(row["ingestion_version"] or ""),
    )
    return MacroEventReleaseInput(
        source_system="fred",
        release_name=label,
        country=country,
        region=region,
        currency=_country_to_currency(country),
        event_type=_category_to_event_type(category),
        scheduled_at_utc=available_at,
        released_at_utc=available_at,
        available_at_utc=available_at,
        model_visible_at_utc=available_at,
        observations=(observation,),
    )


def _macro_identity_payload_from_economic_row(row: sqlite3.Row, metadata: dict[str, Any]) -> MacroEventReleaseInput:
    series_id = str(row["canonical_series_id"] or "").upper()
    meta = metadata.get(series_id)
    label = str(getattr(meta, "label", "") or series_id)
    region = str(row["region"] or getattr(meta, "region", "") or "")
    category = str(row["category"] or getattr(meta, "category", "") or "")
    country = _region_to_country(region)
    available_at = _date_to_conservative_available_at(str(row["observation_date"] or ""))
    observation = MacroObservationInput(
        series_id=series_id,
        indicator_name=label,
        period=str(row["observation_date"] or ""),
        vintage_date=str(row["realtime_start"] or row["observation_date"] or ""),
        vintage_quality=_vintage_quality_for_row(row, provider=str(row["provider"] or "")),
        data_source=str(row["provider"] or ""),
        data_version=str(row["ingestion_version"] or ""),
    )
    return MacroEventReleaseInput(
        source_system=str(row["provider"] or "economic_indicator"),
        release_name=label,
        country=country,
        region=region,
        currency=_country_to_currency(country),
        event_type=_category_to_event_type(category),
        scheduled_at_utc=available_at,
        released_at_utc=available_at,
        available_at_utc=available_at,
        model_visible_at_utc=available_at,
        observations=(observation,),
    )


def _date_in_window(date_text: str, start: str | None, end: str | None) -> bool:
    date_key = _safe_date(date_text)
    if start and date_key < start[:10]:
        return False
    if end and date_key > end[:10]:
        return False
    return True


def _fomc_utc(date_text: str, hour: int = 14, minute: int = 0) -> str:
    year, month, day = (int(part) for part in date_text.split("-"))
    return _to_utc_iso(datetime(year, month, day, hour, minute, tzinfo=EASTERN))


def _fomc_seed_payloads(start: str | None, end: str | None) -> list[MacroEventReleaseInput]:
    """Return Federal Reserve FOMC calendar events for the S04 backfill window.

    The dates are seeded from the Federal Reserve FOMC meeting calendars page.
    This is a source-specific calendar seed, not a fake news row.
    """

    meetings = [
        ("2025-01-29", False, "2025-02-19"),
        ("2025-03-19", True, "2025-04-09"),
        ("2025-05-07", False, "2025-05-28"),
        ("2025-06-18", True, "2025-07-09"),
        ("2025-07-30", False, "2025-08-20"),
        ("2025-09-17", True, "2025-10-08"),
        ("2025-10-29", False, "2025-11-19"),
        ("2025-12-10", True, "2025-12-30"),
        ("2026-01-28", False, "2026-02-18"),
        ("2026-03-18", True, "2026-04-08"),
        ("2026-04-29", False, "2026-05-20"),
        ("2026-06-17", True, "2026-07-08"),
        ("2026-07-29", False, ""),
        ("2026-09-16", True, ""),
        ("2026-10-28", False, ""),
        ("2026-12-09", True, ""),
    ]
    payloads: list[MacroEventReleaseInput] = []

    def build(date_text: str, event_type: str, name: str, *, sep: bool = False) -> MacroEventReleaseInput:
        event_time = _fomc_utc(date_text)
        return MacroEventReleaseInput(
            source_system="federal_reserve",
            release_name=name,
            country="US",
            region="US",
            currency="USD",
            event_type=event_type,
            scheduled_at_utc=event_time,
            released_at_utc=event_time,
            available_at_utc=event_time,
            model_visible_at_utc=event_time,
            release_time_quality="official_exact",
            importance="tier1",
            expected_importance="tier1",
            source_calendar="federal_reserve_fomc_calendar",
            calendar_version=S04_FOMC_SEED_VERSION,
            source_url=FOMC_CALENDAR_SOURCE_URL,
            raw_json={
                "source": "Federal Reserve FOMC meeting calendars",
                "meeting_date": date_text,
                "official_release_time_local": "14:00 America/New_York",
                "sep_associated": sep,
                "seed_version": S04_FOMC_SEED_VERSION,
            },
            observations=(),
            central_bank="Fed",
            central_bank_payload={
                "decision_type": event_type,
                "projection_json": {"sep_associated": sep} if sep else {},
                "text_classifier_version": S04_FOMC_SEED_VERSION,
            },
        )

    for decision_date, sep, minutes_date in meetings:
        if _date_in_window(decision_date, start, end):
            payloads.append(build(decision_date, "central_bank_decision", "FOMC policy decision", sep=sep))
            if sep:
                payloads.append(build(decision_date, "central_bank_projection", "FOMC Summary of Economic Projections", sep=sep))
        if minutes_date and _date_in_window(minutes_date, start, end):
            payloads.append(build(minutes_date, "central_bank_minutes", "FOMC meeting minutes", sep=sep))
    return payloads


def _local_date_time_utc(date_text: str, timezone: ZoneInfo, hour: int, minute: int, second: int = 0) -> str:
    year, month, day = (int(part) for part in date_text.split("-"))
    return _to_utc_iso(datetime(year, month, day, hour, minute, second, tzinfo=timezone))


def _ecb_seed_payloads(start: str | None, end: str | None) -> list[MacroEventReleaseInput]:
    """Seed official ECB decisions/statements/press conferences for global_macro_v1.

    ECB decisions are published at 14:15 local time and the press conference
    starts at 14:45 local time.  The dates are from the ECB Governing Council
    monetary-policy decision calendar; these rows are not synthetic news.
    """

    decision_dates = (
        "2025-01-30",
        "2025-03-06",
        "2025-04-17",
        "2025-06-05",
        "2025-07-24",
        "2025-09-11",
        "2025-10-30",
        "2025-12-18",
        "2026-02-05",
        "2026-03-19",
        "2026-04-30",
        "2026-06-11",
    )
    payloads: list[MacroEventReleaseInput] = []

    def build(date_text: str, event_type: str, name: str, hour: int, minute: int) -> MacroEventReleaseInput:
        event_time = _local_date_time_utc(date_text, EUROPE_BERLIN, hour, minute)
        return MacroEventReleaseInput(
            source_system="ecb",
            release_name=name,
            country="EU",
            region="Europe",
            currency="EUR",
            event_type=event_type,
            scheduled_at_utc=event_time,
            released_at_utc=event_time,
            available_at_utc=event_time,
            model_visible_at_utc=event_time,
            release_time_quality="official_exact",
            importance="tier1",
            expected_importance="tier1",
            source_calendar="ecb_governing_council_monetary_policy_calendar",
            calendar_version=S04P1_ECB_SEED_VERSION,
            source_url=ECB_CALENDAR_SOURCE_URL,
            raw_json={
                "source": "ECB monetary-policy decision calendar",
                "event_date": date_text,
                "official_release_time_local": f"{hour:02d}:{minute:02d} Europe/Berlin",
                "seed_version": S04P1_ECB_SEED_VERSION,
            },
            observations=(),
            central_bank="ECB",
            central_bank_payload={
                "decision_type": event_type,
                "text_classifier_version": S04P1_ECB_SEED_VERSION,
            },
        )

    for date_text in decision_dates:
        if not _date_in_window(date_text, start, end):
            continue
        payloads.extend(
            (
                build(date_text, "central_bank_decision", "ECB monetary policy decision", 14, 15),
                build(date_text, "central_bank_statement", "ECB monetary policy statement", 14, 45),
                build(date_text, "central_bank_press_conference", "ECB monetary policy press conference", 14, 45),
            )
        )
    return payloads


def _boj_seed_payloads(start: str | None, end: str | None) -> list[MacroEventReleaseInput]:
    """Seed BOJ MPM decisions/statements and exact 08:50 JST publications.

    MPM decision/statement publication times are not pre-announced, so their
    conservative visibility is end-of-date and they are never intraday-label
    eligible.  Summary/minutes publication times are officially 08:50 JST.
    """

    decision_dates = (
        "2025-01-24",
        "2025-03-19",
        "2025-05-01",
        "2025-06-17",
        "2025-07-31",
        "2025-09-19",
        "2025-10-30",
        "2025-12-19",
        "2026-01-23",
        "2026-03-19",
        "2026-04-28",
        "2026-06-16",
    )
    summary_dates = (
        "2025-02-03",
        "2025-03-28",
        "2025-05-13",
        "2025-06-25",
        "2025-08-08",
        "2025-09-30",
        "2025-11-10",
        "2025-12-29",
        "2026-02-02",
        "2026-03-30",
        "2026-05-12",
        "2026-06-24",
    )
    minutes_dates = (
        "2025-03-25",
        "2025-05-08",
        "2025-06-20",
        "2025-08-05",
        "2025-09-25",
        "2025-11-05",
        "2025-12-24",
        "2026-01-28",
        "2026-03-25",
        "2026-05-07",
        "2026-06-19",
    )
    payloads: list[MacroEventReleaseInput] = []

    def build(
        date_text: str,
        event_type: str,
        name: str,
        *,
        exact_time: bool,
        decision_type: str,
    ) -> MacroEventReleaseInput:
        scheduled = _local_date_time_utc(date_text, ASIA_TOKYO, 0, 0)
        visible = _local_date_time_utc(date_text, ASIA_TOKYO, 8, 50) if exact_time else _local_date_time_utc(
            date_text, ASIA_TOKYO, 23, 59, 59
        )
        return MacroEventReleaseInput(
            source_system="boj",
            release_name=name,
            country="JP",
            region="Japan",
            currency="JPY",
            event_type=event_type,
            scheduled_at_utc=scheduled,
            released_at_utc=visible,
            available_at_utc=visible,
            model_visible_at_utc=visible,
            release_time_quality="official_exact" if exact_time else "official_date_only",
            importance="tier1",
            expected_importance="tier1",
            source_calendar="boj_monetary_policy_meeting_release_schedule",
            calendar_version=S04P1_BOJ_SEED_VERSION,
            source_url=BOJ_CALENDAR_SOURCE_URL,
            raw_json={
                "source": "Bank of Japan MPM release schedule",
                "event_date": date_text,
                "official_release_time_local": "08:50 Asia/Tokyo" if exact_time else "date only; conservative end-of-date visibility",
                "seed_version": S04P1_BOJ_SEED_VERSION,
            },
            observations=(),
            central_bank="BOJ",
            central_bank_payload={
                "decision_type": decision_type,
                "text_classifier_version": S04P1_BOJ_SEED_VERSION,
            },
        )

    for date_text in decision_dates:
        if not _date_in_window(date_text, start, end):
            continue
        payloads.extend(
            (
                build(
                    date_text,
                    "central_bank_decision",
                    "BOJ monetary policy decision",
                    exact_time=False,
                    decision_type="central_bank_decision",
                ),
                build(
                    date_text,
                    "central_bank_statement",
                    "BOJ statement on monetary policy",
                    exact_time=False,
                    decision_type="central_bank_statement",
                ),
            )
        )
    for date_text in summary_dates:
        if _date_in_window(date_text, start, end):
            payloads.append(
                build(
                    date_text,
                    "central_bank_minutes",
                    "BOJ Summary of Opinions",
                    exact_time=True,
                    decision_type="central_bank_summary_of_opinions",
                )
            )
    for date_text in minutes_dates:
        if _date_in_window(date_text, start, end):
            payloads.append(
                build(
                    date_text,
                    "central_bank_minutes",
                    "BOJ monetary policy meeting minutes",
                    exact_time=True,
                    decision_type="central_bank_minutes",
                )
            )
    return payloads


def _macro_payload_from_fred_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    metadata: dict[str, Any],
) -> MacroEventReleaseInput:
    series_id = str(row["series_id"] or "").upper()
    meta = metadata.get(series_id)
    label = str(getattr(meta, "label", "") or series_id)
    region = str(getattr(meta, "region", "") or "")
    category = str(getattr(meta, "category", "") or "")
    country = _region_to_country(region)
    available_at = _date_to_conservative_available_at(str(row["observation_date"] or ""))
    prior = _prior_for_series(conn, "fred_observations", "series_id", series_id, str(row["observation_date"] or ""))
    value = float(row["value"])
    observation = MacroObservationInput(
        series_id=series_id,
        indicator_name=label,
        actual_value=value,
        prior_value=prior,
        unit="",
        period=str(row["observation_date"] or ""),
        frequency="",
        seasonal_adjustment="",
        surprise_raw=(value - prior) if prior is not None else None,
        vintage_date=str(row["realtime_start"] or row["observation_date"] or ""),
        vintage_quality=_vintage_quality_for_row(row, provider="fred"),
        data_source="fred",
        data_version=str(row["ingestion_version"] or ""),
        raw_json=dict(row),
    )
    return MacroEventReleaseInput(
        source_system="fred",
        release_name=label,
        country=country,
        region=region,
        currency=_country_to_currency(country),
        event_type=_category_to_event_type(category),
        scheduled_at_utc=available_at,
        released_at_utc=available_at,
        available_at_utc=available_at,
        model_visible_at_utc=available_at,
        release_time_quality="observation_date_only",
        importance=f"tier{getattr(meta, 'importance', '') or 'unknown'}",
        expected_importance=f"tier{getattr(meta, 'importance', '') or 'unknown'}",
        source_calendar="fred_observations",
        source_url=str(row["source_url"] or ""),
        raw_json={"source_table": "fred_observations", "row": dict(row), "metadata": getattr(meta, "__dict__", {}) if meta else {}},
        observations=(observation,),
    )


def _macro_payload_from_economic_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    metadata: dict[str, Any],
) -> MacroEventReleaseInput:
    series_id = str(row["canonical_series_id"] or "").upper()
    meta = metadata.get(series_id)
    label = str(getattr(meta, "label", "") or series_id)
    region = str(row["region"] or getattr(meta, "region", "") or "")
    category = str(row["category"] or getattr(meta, "category", "") or "")
    country = _region_to_country(region)
    available_at = _date_to_conservative_available_at(str(row["observation_date"] or ""))
    prior = _prior_for_series(conn, "economic_indicator_observations", "canonical_series_id", series_id, str(row["observation_date"] or ""))
    value = float(row["value"])
    observation = MacroObservationInput(
        series_id=series_id,
        indicator_name=label,
        actual_value=value,
        prior_value=prior,
        unit=str(row["unit"] or ""),
        period=str(row["observation_date"] or ""),
        frequency=str(row["frequency"] or ""),
        seasonal_adjustment="",
        surprise_raw=(value - prior) if prior is not None else None,
        vintage_date=str(row["realtime_start"] or row["observation_date"] or ""),
        vintage_quality=_vintage_quality_for_row(row, provider=str(row["provider"] or "")),
        data_source=str(row["provider"] or ""),
        data_version=str(row["ingestion_version"] or ""),
        raw_json=json.loads(row["raw_json"]) if str(row["raw_json"] or "").strip().startswith("{") else dict(row),
    )
    return MacroEventReleaseInput(
        source_system=str(row["provider"] or "economic_indicator"),
        release_name=label,
        country=country,
        region=region,
        currency=_country_to_currency(country),
        event_type=_category_to_event_type(category),
        scheduled_at_utc=available_at,
        released_at_utc=available_at,
        available_at_utc=available_at,
        model_visible_at_utc=available_at,
        release_time_quality="observation_date_only",
        importance=f"tier{getattr(meta, 'importance', '') or 'unknown'}",
        expected_importance=f"tier{getattr(meta, 'importance', '') or 'unknown'}",
        source_calendar="economic_indicator_observations",
        source_url=str(row["source_url"] or ""),
        raw_json={"source_table": "economic_indicator_observations", "row": dict(row), "metadata": getattr(meta, "__dict__", {}) if meta else {}},
        observations=(observation,),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _ensure_macro_source_indexes(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "fred_observations"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fred_observations_series_date_id "
            "ON fred_observations(series_id,observation_date,id)"
        )
    if _table_exists(conn, "economic_indicator_observations"):
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_econ_obs_series_date_priority_id "
            "ON economic_indicator_observations(canonical_series_id,observation_date,provider_priority,id)"
        )


def rebuild_macro_event_lineage_from_observations(
    database_path: str,
    *,
    source: str = "all",
    start: str | None = None,
    end: str | None = None,
    limit: int | None = None,
    universe: str = "global_core",
) -> dict[str, Any]:
    """Promote stored macro observations into first-class event clusters.

    This bridge is intentionally local-only: it does not refresh FRED,
    ALFRED, EIA, or any other network source.  It turns the already stored
    `fred_observations` / `economic_indicator_observations` rows into M251B
    `macro_event_releases` with conservative time-quality/vintage flags.
    """

    from pathlib import Path

    from .macro_universe import metadata_by_series

    db_path = Path(database_path)
    conn = sqlite3.connect(db_path, timeout=60, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        ensure_macro_event_lineage_schema(conn)
        _ensure_macro_source_indexes(conn)
        metadata = metadata_by_series(universe)
        normalized_source = str(source or "all").lower()
        stored: list[int] = []
        source_counts: dict[str, int] = {}
        limit_remaining = int(limit) if limit is not None and int(limit) > 0 else None
        conn.execute("BEGIN IMMEDIATE")

        if normalized_source in {"all", "fred"} and _table_exists(conn, "fred_observations") and (limit_remaining is None or limit_remaining > 0):
            where = []
            params: list[Any] = []
            if start:
                where.append("observation_date>=?")
                params.append(start[:10])
            if end:
                where.append("observation_date<=?")
                params.append(end[:10])
            sql = "SELECT * FROM fred_observations"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY observation_date ASC,series_id ASC,id ASC"
            if limit_remaining is not None:
                sql += " LIMIT ?"
                params.append(limit_remaining)
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                existing_id = _existing_macro_event_id(conn, _macro_identity_payload_from_fred_row(row, metadata))
                if existing_id is not None:
                    stored.append(existing_id)
                    continue
                stored.append(upsert_macro_event_release(conn, _macro_payload_from_fred_row(conn, row, metadata)))
            source_counts["fred"] = len(rows)
            if limit_remaining is not None:
                limit_remaining -= len(rows)

        if normalized_source in {"all", "economic", "economic_indicators"} and _table_exists(conn, "economic_indicator_observations") and (limit_remaining is None or limit_remaining > 0):
            where = []
            params = []
            if start:
                where.append("observation_date>=?")
                params.append(start[:10])
            if end:
                where.append("observation_date<=?")
                params.append(end[:10])
            sql = "SELECT * FROM economic_indicator_observations"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY observation_date ASC,canonical_series_id ASC,provider_priority ASC,id ASC"
            if limit_remaining is not None:
                sql += " LIMIT ?"
                params.append(limit_remaining)
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                existing_id = _existing_macro_event_id(conn, _macro_identity_payload_from_economic_row(row, metadata))
                if existing_id is not None:
                    stored.append(existing_id)
                    continue
                stored.append(upsert_macro_event_release(conn, _macro_payload_from_economic_row(conn, row, metadata)))
            source_counts["economic_indicators"] = len(rows)

        if normalized_source in {"all", "fomc", "federal_reserve", "central_bank"}:
            fomc_payloads = _fomc_seed_payloads(start, end)
            for payload in fomc_payloads:
                stored.append(upsert_macro_event_release(conn, payload))
            source_counts["federal_reserve_fomc"] = len(fomc_payloads)

        if normalized_source in {"all", "ecb", "central_bank"}:
            ecb_payloads = _ecb_seed_payloads(start, end)
            for payload in ecb_payloads:
                stored.append(upsert_macro_event_release(conn, payload))
            source_counts["ecb_official_calendar"] = len(ecb_payloads)

        if normalized_source in {"all", "boj", "central_bank"}:
            boj_payloads = _boj_seed_payloads(start, end)
            for payload in boj_payloads:
                stored.append(upsert_macro_event_release(conn, payload))
            source_counts["boj_official_calendar"] = len(boj_payloads)

        total_clusters = conn.execute("SELECT COUNT(*) AS c FROM news_event_clusters WHERE cluster_type IN ('macro_release','central_bank_event')").fetchone()["c"]
        total_releases = conn.execute("SELECT COUNT(*) AS c FROM macro_event_releases").fetchone()["c"]
        total_central_bank = conn.execute("SELECT COUNT(*) AS c FROM central_bank_events").fetchone()["c"]
        quality_rows = [
            dict(row)
            for row in conn.execute(
                """SELECT vintage_quality,COUNT(*) AS count
                     FROM macro_observations
                    GROUP BY vintage_quality
                    ORDER BY vintage_quality"""
            ).fetchall()
        ]
        result = {
            "version": MACRO_EVENT_LINEAGE_VERSION,
            "database_path": str(db_path),
            "generated_at": _utc_now_iso(),
            "source": normalized_source,
            "start": start,
            "end": end,
            "limit": limit,
            "universe": universe,
            "input_rows_seen": source_counts,
            "macro_events_touched": len(set(stored)),
            "macro_event_releases_total": int(total_releases),
            "macro_event_clusters_total": int(total_clusters),
            "central_bank_events_total": int(total_central_bank),
            "vintage_quality_counts": quality_rows,
            "safety": {
                "network_fetch": False,
                "broker_orders_submitted": False,
                "transmit_enabled": False,
                "live_order_routing_enabled": False,
            },
        }
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def render_macro_event_lineage_rebuild_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# M251B Macro Event Lineage Rebuild",
        "",
        f"- Generated: {payload.get('generated_at')}",
        f"- Version: `{payload.get('version')}`",
        f"- Database: `{payload.get('database_path')}`",
        f"- Source: `{payload.get('source')}`",
        f"- Window: `{payload.get('start') or 'all'}` -> `{payload.get('end') or 'all'}`",
        f"- Input rows seen: `{payload.get('input_rows_seen')}`",
        f"- Macro events touched: {payload.get('macro_events_touched')}",
        f"- Macro releases total: {payload.get('macro_event_releases_total')}",
        f"- Macro clusters total: {payload.get('macro_event_clusters_total')}",
        f"- Central bank events total: {payload.get('central_bank_events_total')}",
        "",
        "## Vintage quality",
        "",
    ]
    quality = payload.get("vintage_quality_counts") or []
    if quality:
        for item in quality:
            lines.append(f"- {item.get('vintage_quality')}: {item.get('count')}")
    else:
        lines.append("- No macro observations linked yet.")
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Local DB bridge only; no macro network refresh.",
            "- No broker order submitted.",
            "- No transmit, live routing, or live threshold promotion.",
        ]
    )
    return "\n".join(lines) + "\n"
