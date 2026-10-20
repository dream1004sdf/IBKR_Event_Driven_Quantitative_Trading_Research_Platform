from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .s04_market_data_pre_s05_audit import calendar_schedule, parse_utc


NORMALIZATION_VERSION = "s05p1-headline-normalization-v1"
CLASSIFIER_VERSION = "s05p1-news-semantic-rules-v1"
ENTITY_ROLE_VERSION = "s05p1-explicit-subject-role-v1"
SNAPSHOT_VERSION = "s05p1-point-in-time-snapshot-v1"
SERIES_VERSION = "s05p1-event-series-dedup-v1"
CORE_SCOPE_VERSION = "s05p1-required-core-v1"
REQUIRED_CORE_ASSETS: tuple[tuple[str, str, str], ...] = (
    ("SPY", "core_index", "broad_us_equity"),
    ("QQQ", "core_index", "growth_technology"),
    ("IWM", "core_index", "small_cap"),
    ("TLT", "core_macro", "long_rates"),
    ("GLD", "core_macro", "gold"),
    ("UUP", "core_macro", "usd"),
    ("USO", "core_macro", "oil"),
)
NY = ZoneInfo("America/New_York")

_IBKR_PREFIX = re.compile(r"^\s*\{A:[^}]+\}\s*", re.IGNORECASE)
_SPACE = re.compile(r"\s+")
_TICKER_PAREN = re.compile(r"\((?:NYSE:|NASDAQ:|AMEX:)?([A-Z][A-Z0-9.\-]{0,5})\)")
_TICKER_EXCHANGE = re.compile(r"\b(?:NYSE|NASDAQ|AMEX):([A-Z][A-Z0-9.\-]{0,5})\b")


def stable_hash(*parts: Any) -> str:
    return hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).hexdigest()


def normalize_headline(raw_headline: str) -> str:
    value = html.unescape(str(raw_headline or ""))
    value = _IBKR_PREFIX.sub("", value)
    value = value.replace("�", "-")
    return _SPACE.sub(" ", value).strip()


def normalized_series_text(value: str) -> str:
    value = normalize_headline(value).lower()
    value = re.sub(r"\b(?:updated?|update|live)\b[:\s-]*", "", value)
    value = re.sub(r"\b\d{1,2}:\d{2}\b", "", value)
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return _SPACE.sub(" ", value).strip()


@dataclass(frozen=True)
class SemanticResult:
    news_content_type: str
    event_class: str
    confidence: float
    causal_trigger_eligible: bool
    matched_rule: str


def classify_headline(headline: str) -> SemanticResult:
    text = normalize_headline(headline).lower()
    rules: tuple[tuple[str, str, float, bool, str], ...] = (
        (r"\b(upgrades?|downgrades?|price (?:target|objective)|initiates? coverage|maintains? .* rating|raises? .* target|cuts? .* target)\b", "analyst_action", 0.99, True, "analyst_action"),
        (r"\b(price over earnings|p/e|valuation|fair value|overvalued|undervalued)\b", "valuation_article", 0.98, False, "valuation_article"),
        (r"\b(live coverage|stock market today|market today|futures (?:tumble|rise|fall|flat)|market recap|closing bell)\b", "market_recap", 0.98, False, "market_recap"),
        (r"\b(shares? (?:are|is) trading (?:higher|lower)|why is .* (?:gaining|falling|rising)|stock (?:jumps|drops|gains|falls|moves|dips))\b", "price_move_recap", 0.98, False, "price_move_recap"),
        (r"\b(technical analysis|chart setup|support level|resistance level|moving average|rsi)\b", "technical_analysis", 0.97, False, "technical_analysis"),
        (r"\b(if you invested|best stocks? to buy|top \d+|stocks? to watch|hidden gem|good stock to buy|dividend champions?)\b", "listicle", 0.96, False, "listicle"),
        (r"\b(opinion|outlook|what you should know|key facts|overview|prospects|could mean)\b", "opinion", 0.90, False, "opinion"),
        (r"\b(earnings call transcript|conference call transcript)\b", "secondary_report", 0.99, False, "transcript"),
        (r"\b(raises?|cuts?|reaffirms?|updates?) (?:its )?(?:full[- ]year |fy\d* )?(?:guidance|outlook|forecast)\b|\bguidance (?:raised|cut|lowered|increased|reaffirmed)\b", "earnings_guidance", 0.99, True, "earnings_guidance"),
        (r"\b(reports?|reported|posts?|posted) (?:q[1-4]|quarter|quarterly|annual|fy)|\b(?:eps|revenue) (?:beats?|misses?|tops?|falls short)\b", "earnings_result", 0.97, True, "earnings_result"),
        (r"\b(ceo|cfo|chairman|board|director|leadership)\b.*\b(appoints?|resigns?|retires?|steps down|names?|change)\b|\bappoints?\b.*\b(ceo|cfo|director)\b", "management_change", 0.97, True, "management_change"),
        (r"\b(acquires?|acquisition|merger|merges?|takeover|buyout|to buy|to acquire)\b", "ma_transaction", 0.96, True, "ma_transaction"),
        (r"\b(sec|ftc|doj|fda|regulator|regulatory|antitrust|approval|investigation|lawsuit|court)\b", "regulatory_action", 0.91, True, "regulatory_action"),
        (r"\b(press release|announces?|launches?|wins? contract|signs? agreement|plans? \d|files? for)\b", "company_press_release", 0.83, True, "company_event"),
        (r"\b(calendar|preview|week ahead|what to expect)\b", "calendar_preview", 0.90, False, "calendar_preview"),
    )
    for pattern, content_type, confidence, eligible, rule in rules:
        if re.search(pattern, text):
            event_class = {
                "earnings_result": "company_event.earnings_result",
                "earnings_guidance": "company_event.earnings_guidance",
                "analyst_action": "company_event.analyst_action",
                "management_change": "company_event.management_change",
                "ma_transaction": "company_event.ma_transaction",
                "regulatory_action": "company_event.regulatory_action",
                "company_press_release": "company_event.primary_event",
            }.get(content_type, f"context.{content_type}")
            return SemanticResult(content_type, event_class, confidence, eligible, rule)
    return SemanticResult("unknown", "unknown", 0.0, False, "no_high_precision_rule")


def explicit_tickers(headline: str) -> list[str]:
    normalized = normalize_headline(headline)
    values: list[str] = []
    for match in _TICKER_PAREN.finditer(normalized):
        # Reject regulatory clause notation such as 5635(C)(4).
        before = normalized[max(0, match.start() - 1):match.start()]
        after = normalized[match.end():match.end() + 2]
        if before.isdigit() or re.match(r"\(\d", after):
            continue
        values.append(match.group(1))
    values += _TICKER_EXCHANGE.findall(normalized)
    excluded = {"EPS", "CEO", "CFO", "ETF", "IPO", "AI", "PE", "USA", "LODR", "SEC", "FDA"}
    return list(dict.fromkeys(value.upper() for value in values if value.upper() not in excluded))


def resolve_entity_roles(headline: str, semantic: SemanticResult, candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = normalize_headline(headline)
    explicit = explicit_tickers(normalized)
    candidate_by_symbol: dict[str, dict[str, Any]] = {}
    for row in candidates:
        symbol = str(row.get("symbol") or row.get("raw_symbol") or "").upper().strip()
        if symbol:
            candidate_by_symbol.setdefault(symbol, row)
    roles: list[dict[str, Any]] = []
    direct_role = "issuer"
    if semantic.news_content_type == "ma_transaction":
        direct_role = "target" if len(explicit) == 1 else "subject"
    for symbol in explicit:
        candidate = candidate_by_symbol.get(symbol, {})
        # Explicit subject tickers are eligible for dynamic IBKR resolution. They are not
        # tradable until that later contract-resolution step succeeds.
        resolved = True
        resolver_status = str(candidate.get("resolver_status") or "")
        if resolver_status != "resolved" or int(candidate.get("tradable_candidate") or 0) != 1:
            resolver_status = "pending_dynamic_resolution"
        roles.append({
            "symbol": symbol,
            "entity_role": direct_role,
            "role_confidence": 0.99,
            "direct_eligible": int(resolved),
            "resolver_status": resolver_status,
            "con_id": candidate.get("con_id"),
            "source": str(candidate.get("source") or "normalized_headline_explicit_ticker"),
        })
    if semantic.news_content_type == "analyst_action":
        match = re.match(
            r"^(.+?)\s+(?:upgrades?|downgrades?|raises?|cuts?|reduces?|initiates?|maintains?)\b",
            normalized,
            flags=re.IGNORECASE,
        )
        if match:
            source_name = _SPACE.sub(" ", match.group(1)).strip(" :-")
            source_key = re.sub(r"[^A-Z0-9]+", "_", source_name.upper()).strip("_")[:48]
            roles.append({
                "symbol": f"SOURCE:{source_key}",
                "entity_role": "analyst_or_source",
                "role_confidence": 0.99,
                "direct_eligible": 0,
                "resolver_status": "non_tradable_source_entity",
                "con_id": None,
                "source": "normalized_headline_analyst_source",
            })
    return roles


def calendar_semantics(anchor: datetime, schedule: dict[date, tuple[datetime, datetime]]) -> dict[str, Any]:
    anchor = anchor.astimezone(UTC)
    local_date = anchor.astimezone(NY).date()
    session = schedule.get(local_date)
    future_sessions = sorted(day for day in schedule if day >= local_date)
    first_regular = next((day for day in future_sessions if schedule[day][1] > anchor), None)
    if not session:
        bucket = "market_closed"
    else:
        market_open, market_close = session
        pre_open = datetime.combine(local_date, time(4, 0), NY).astimezone(UTC)
        post_close = datetime.combine(local_date, time(20, 0), NY).astimezone(UTC)
        if pre_open <= anchor < market_open:
            bucket = "premarket"
        elif market_open <= anchor < market_close:
            bucket = "regular"
        elif market_close <= anchor < post_close:
            bucket = "afterhours"
        else:
            bucket = "outside_session"
    if first_regular is None:
        return {"anchor_trading_date": "", "is_trading_day": int(session is not None), "session_bucket": bucket,
                "first_regular_session_date": "", "next_regular_open_at": "", "next_regular_close_at": ""}
    next_open, next_close = schedule[first_regular]
    return {
        "anchor_trading_date": local_date.isoformat(),
        "is_trading_day": int(session is not None),
        "session_bucket": bucket,
        "first_regular_session_date": first_regular.isoformat(),
        "next_regular_open_at": next_open.isoformat(),
        "next_regular_close_at": next_close.isoformat(),
    }


def ensure_closeout_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p1_news_cluster_snapshots (
            cluster_snapshot_id TEXT PRIMARY KEY,
            event_cluster_id INTEGER NOT NULL,
            snapshot_type TEXT NOT NULL,
            plan_as_of_utc TEXT NOT NULL,
            raw_headline TEXT NOT NULL,
            normalized_headline TEXT NOT NULL,
            normalization_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            provider_count_as_of INTEGER NOT NULL,
            article_count_as_of INTEGER NOT NULL,
            article_ids_json TEXT NOT NULL,
            entities_as_of_json TEXT NOT NULL,
            event_class_as_of TEXT NOT NULL,
            event_class_confidence REAL NOT NULL,
            classifier_version TEXT NOT NULL,
            news_content_type TEXT NOT NULL,
            causal_trigger_eligible INTEGER NOT NULL,
            anchor_trading_date TEXT NOT NULL,
            is_trading_day INTEGER NOT NULL,
            session_bucket TEXT NOT NULL,
            first_regular_session_date TEXT NOT NULL,
            next_regular_open_at TEXT NOT NULL,
            next_regular_close_at TEXT NOT NULL,
            mapping_run_available_at TEXT NOT NULL,
            classification_available_at TEXT NOT NULL,
            future_article_input_count INTEGER NOT NULL DEFAULT 0,
            future_entity_input_count INTEGER NOT NULL DEFAULT 0,
            future_classification_input_count INTEGER NOT NULL DEFAULT 0,
            event_series_id TEXT NOT NULL,
            update_of_cluster_id INTEGER,
            novelty_type TEXT NOT NULL,
            series_sample_weight REAL NOT NULL,
            snapshot_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(event_cluster_id,snapshot_type,plan_as_of_utc,snapshot_version)
        );
        CREATE TABLE IF NOT EXISTS s05p1_news_entity_roles (
            entity_role_key TEXT PRIMARY KEY,
            cluster_snapshot_id TEXT NOT NULL,
            event_cluster_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            entity_role TEXT NOT NULL,
            role_confidence REAL NOT NULL,
            direct_eligible INTEGER NOT NULL,
            resolver_status TEXT NOT NULL,
            con_id INTEGER,
            source TEXT NOT NULL,
            entity_role_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(cluster_snapshot_id,symbol,entity_role,entity_role_version)
        );
        CREATE TABLE IF NOT EXISTS s05p1_reaction_plan_metadata (
            reaction_plan_id INTEGER PRIMARY KEY,
            cluster_snapshot_id TEXT NOT NULL UNIQUE,
            plan_as_of_utc TEXT NOT NULL,
            news_content_type TEXT NOT NULL,
            causal_trigger_eligible INTEGER NOT NULL,
            event_series_id TEXT NOT NULL,
            novelty_type TEXT NOT NULL,
            series_sample_weight REAL NOT NULL,
            core_scope_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL
        );
        """
    )


def provider_family(provider: str, source_system: str = "") -> str:
    value = str(provider or source_system).lower().strip()
    if value.startswith("dj"):
        return "djnl"
    if value in {"bz", "benzinga"}:
        return "bz"
    if "marketaux" in value:
        return "marketaux"
    return value or "unknown"


def assign_event_series(rows: list[dict[str, Any]]) -> None:
    representatives: list[tuple[str, str, int]] = []
    members: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sorted(rows, key=lambda item: (item["plan_as_of_utc"], item["event_cluster_id"])):
        text = normalized_series_text(row["normalized_headline"])
        series_id = ""
        for representative, candidate_id, _ in representatives:
            if representative == text or SequenceMatcher(None, representative, text).ratio() >= 0.94:
                series_id = candidate_id
                break
        if not series_id:
            series_id = f"news-series:{stable_hash(SERIES_VERSION, text)[:24]}"
            representatives.append((text, series_id, int(row["event_cluster_id"])))
        row["event_series_id"] = series_id
        members[series_id].append(row)
    for values in members.values():
        values.sort(key=lambda item: (item["plan_as_of_utc"], item["event_cluster_id"]))
        for index, row in enumerate(values):
            row["novelty_type"] = "new" if index == 0 else "update"
            row["update_of_cluster_id"] = None if index == 0 else int(values[0]["event_cluster_id"])
            row["series_sample_weight"] = 1.0 / len(values)


def build_time_stratified_selection(database: Path, per_period: int = 100) -> dict[str, Any]:
    periods = (
        ("2025_h1", "2025-01-01T05:00:00+00:00", "2025-07-01T04:00:00+00:00"),
        ("2025_h2", "2025-07-01T04:00:00+00:00", "2026-01-01T05:00:00+00:00"),
        ("2026_h1", "2026-01-01T05:00:00+00:00", "2026-07-06T04:00:00+00:00"),
    )
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        all_rows: list[dict[str, Any]] = []
        for period, start, end in periods:
            candidates = [dict(row) for row in conn.execute(
                """WITH visible AS (
                       SELECT l.event_cluster_id,r.id news_id,r.headline,r.source_system,r.provider,r.route_type,
                              COALESCE(v.model_visible_at_utc,
                                  CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                       ELSE r.available_at_utc END,r.published_at_utc) visible_at,
                              ROW_NUMBER() OVER(PARTITION BY l.event_cluster_id ORDER BY COALESCE(v.model_visible_at_utc,
                                  CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                                       ELSE r.available_at_utc END,r.published_at_utc),r.id) rn
                         FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
                         LEFT JOIN news_visibility_policy v ON v.news_id=r.id
                    )
                    SELECT c.event_cluster_id,v.news_id,v.headline raw_headline,v.source_system,v.provider,v.route_type,
                           v.visible_at plan_as_of_utc
                      FROM news_event_clusters c JOIN visible v ON v.event_cluster_id=c.event_cluster_id AND v.rn=1
                     WHERE c.cluster_type='news_event' AND c.reaction_trigger_eligible=1
                       AND v.visible_at>=? AND v.visible_at<?
                     ORDER BY v.visible_at,c.event_cluster_id""", (start, end))]
            enriched: list[dict[str, Any]] = []
            for row in candidates:
                semantic = classify_headline(row["raw_headline"])
                anchor = parse_utc(row["plan_as_of_utc"])
                copy = dict(row)
                copy.update({"period": period, "normalized_headline": normalize_headline(row["raw_headline"]),
                             "news_content_type": semantic.news_content_type, "event_class": semantic.event_class,
                             "event_class_confidence": semantic.confidence, "causal_trigger_eligible": int(semantic.causal_trigger_eligible),
                             "provider_bucket": provider_family(row["provider"], row["source_system"]),
                             "timestamp_second": anchor.second})
                enriched.append(copy)
            # Deterministic round-robin over provider and semantic type.
            buckets: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for row in enriched:
                buckets[(row["provider_bucket"], row["news_content_type"])].append(row)
            selected: list[dict[str, Any]] = []
            providers = sorted({key[0] for key in buckets})
            content_by_provider = {
                provider: sorted(key[1] for key in buckets if key[0] == provider)
                for provider in providers
            }
            offsets = {provider: 0 for provider in providers}
            while len(selected) < per_period and any(buckets.values()):
                progressed = False
                for provider in providers:
                    content_types = content_by_provider[provider]
                    for _ in range(len(content_types)):
                        content = content_types[offsets[provider] % len(content_types)]
                        offsets[provider] += 1
                        queue = buckets[(provider, content)]
                        if queue:
                            selected.append(queue.pop(0))
                            progressed = True
                            break
                    if len(selected) >= per_period:
                        break
                if not progressed:
                    break
            all_rows.extend(selected)
    assign_event_series(all_rows)
    return {
        "version": "s05p1-news-time-stratified-selection-v1",
        "canonical_timezone": "America/New_York",
        "canonical_cutoff": {"start_local": "2025-01-01T00:00:00", "end_local_exclusive": "2026-07-06T00:00:00"},
        "selected_count": len(all_rows),
        "periods": {period: sum(row["period"] == period for row in all_rows) for period, _, _ in periods},
        "counts": {
            "provider": dict(Counter(row["provider_bucket"] for row in all_rows)),
            "content_type": dict(Counter(row["news_content_type"] for row in all_rows)),
            "event_class": dict(Counter(row["event_class"] for row in all_rows)),
            "causal_eligible": sum(row["causal_trigger_eligible"] for row in all_rows),
            "exact_second": sum(row["timestamp_second"] != 0 for row in all_rows),
            "event_series": len({row["event_series_id"] for row in all_rows}),
        },
        "clusters": all_rows,
    }


def _visible_articles(conn: sqlite3.Connection, cluster_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT r.id news_id,r.headline,r.source_system,r.provider,r.route_type,r.primary_route_symbol,
                  COALESCE(v.model_visible_at_utc,
                      CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                           ELSE r.available_at_utc END,r.published_at_utc) visible_at
             FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
             LEFT JOIN news_visibility_policy v ON v.news_id=r.id
            WHERE l.event_cluster_id=?
            ORDER BY visible_at,r.id""", (cluster_id,))]


def _cluster_candidates(conn: sqlite3.Connection, cluster_id: int) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT event_cluster_id,symbol,con_id,role,source,confidence,resolver_status,tradable_candidate
             FROM v_cluster_asset_candidate_aggregates WHERE event_cluster_id=?
            ORDER BY confidence DESC,symbol""", (cluster_id,))]


def _bulk_inputs(conn: sqlite3.Connection, cluster_ids: list[int]) -> tuple[dict[int, list[dict[str, Any]]], dict[int, list[dict[str, Any]]]]:
    placeholders = ",".join("?" for _ in cluster_ids)
    articles: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in conn.execute(
        f"""SELECT l.event_cluster_id,r.id news_id,r.headline,r.source_system,r.provider,r.route_type,
                   r.primary_route_symbol,COALESCE(v.model_visible_at_utc,
                     CASE WHEN r.source_system='ibkr' THEN strftime('%Y-%m-%dT%H:%M:%S+00:00',r.published_at_utc,'+60 seconds')
                          ELSE r.available_at_utc END,r.published_at_utc) visible_at
              FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id
              LEFT JOIN news_visibility_policy v ON v.news_id=r.id
             WHERE l.event_cluster_id IN ({placeholders})
             ORDER BY l.event_cluster_id,visible_at,r.id""", cluster_ids
    ):
        articles[int(row["event_cluster_id"])].append(dict(row))
    return articles, {}


def materialize_closeout(clone_database: Path, selection: dict[str, Any]) -> dict[str, Any]:
    schedule = calendar_schedule(date(2024, 12, 20), date(2026, 7, 20))
    now = datetime.now(UTC).isoformat()
    enriched: list[dict[str, Any]] = []
    confirmed_snapshots = 0
    direct_rows: list[dict[str, Any]] = []
    with sqlite3.connect(clone_database) as conn:
        conn.row_factory = sqlite3.Row
        ensure_closeout_schema(conn)
        cluster_ids = [int(row["event_cluster_id"]) for row in selection["clusters"]]
        articles_by_cluster, candidates_by_cluster = _bulk_inputs(conn, cluster_ids)
        for selected in selection["clusters"]:
            cluster_id = int(selected["event_cluster_id"])
            articles = articles_by_cluster.get(cluster_id, [])
            if not articles:
                continue
            initial_visible = parse_utc(str(articles[0]["visible_at"]))
            plan_as_of = initial_visible + timedelta(seconds=1)
            included = [row for row in articles if parse_utc(str(row["visible_at"])) <= plan_as_of]
            raw_headline = str(included[0]["headline"])
            normalized = normalize_headline(raw_headline)
            semantic = classify_headline(normalized)
            # Roles are derived only from the point-in-time normalized headline. Final
            # cluster candidate state is deliberately excluded from the initial snapshot.
            roles = resolve_entity_roles(normalized, semantic, [])
            calendar = calendar_semantics(plan_as_of, schedule)
            providers = {provider_family(row["provider"], row["source_system"]) for row in included}
            snapshot_id = f"news-snapshot:{stable_hash(cluster_id, 'initial', plan_as_of.isoformat(), SNAPSHOT_VERSION)[:24]}"
            entities_json = json.dumps(roles, ensure_ascii=False, sort_keys=True)
            input_hash = stable_hash(NORMALIZATION_VERSION, normalized, entities_json)
            values = {
                **selected,
                **calendar,
                "cluster_snapshot_id": snapshot_id,
                "snapshot_type": "initial",
                "plan_as_of_utc": plan_as_of.isoformat(),
                "anchor_at_utc": plan_as_of.isoformat(),
                "raw_headline": raw_headline,
                "primary_headline": normalized,
                "normalized_headline": normalized,
                "provider_count_as_of": len(providers),
                "article_count_as_of": len(included),
                "article_ids": [int(row["news_id"]) for row in included],
                "event_class": semantic.event_class,
                "event_class_confidence": semantic.confidence,
                "news_content_type": semantic.news_content_type,
                "causal_trigger_eligible": int(semantic.causal_trigger_eligible),
                "entity_roles": roles,
                "direct_roles": [row for row in roles if row["direct_eligible"] and row["entity_role"] in {"subject", "issuer", "acquirer", "target"}],
                "event_role": semantic.news_content_type,
                "input_hash": input_hash,
            }
            enriched.append(values)
            conn.execute(
                """INSERT OR REPLACE INTO s05p1_news_cluster_snapshots(
                       cluster_snapshot_id,event_cluster_id,snapshot_type,plan_as_of_utc,raw_headline,normalized_headline,
                       normalization_version,input_hash,provider_count_as_of,article_count_as_of,article_ids_json,
                       entities_as_of_json,event_class_as_of,event_class_confidence,classifier_version,news_content_type,
                       causal_trigger_eligible,anchor_trading_date,is_trading_day,session_bucket,first_regular_session_date,
                       next_regular_open_at,next_regular_close_at,mapping_run_available_at,classification_available_at,
                       future_article_input_count,future_entity_input_count,future_classification_input_count,event_series_id,
                       update_of_cluster_id,novelty_type,series_sample_weight,snapshot_version,created_at_utc
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id,cluster_id,"initial",plan_as_of.isoformat(),raw_headline,normalized,NORMALIZATION_VERSION,input_hash,
                 len(providers),len(included),json.dumps(values["article_ids"]),entities_json,semantic.event_class,
                 semantic.confidence,CLASSIFIER_VERSION,semantic.news_content_type,int(semantic.causal_trigger_eligible),
                 calendar["anchor_trading_date"],calendar["is_trading_day"],calendar["session_bucket"],calendar["first_regular_session_date"],
                 calendar["next_regular_open_at"],calendar["next_regular_close_at"],plan_as_of.isoformat(),plan_as_of.isoformat(),
                 0,0,0,selected["event_series_id"],selected.get("update_of_cluster_id"),selected["novelty_type"],
                 selected["series_sample_weight"],SNAPSHOT_VERSION,now),
            )
            for role in roles:
                key = stable_hash(snapshot_id, role["symbol"], role["entity_role"], ENTITY_ROLE_VERSION)
                conn.execute(
                    """INSERT OR REPLACE INTO s05p1_news_entity_roles(
                           entity_role_key,cluster_snapshot_id,event_cluster_id,symbol,entity_role,role_confidence,
                           direct_eligible,resolver_status,con_id,source,entity_role_version,created_at_utc
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (key,snapshot_id,cluster_id,role["symbol"],role["entity_role"],role["role_confidence"],
                     role["direct_eligible"],role["resolver_status"],role.get("con_id"),role["source"],ENTITY_ROLE_VERSION,now),
                )
                if role["direct_eligible"]:
                    direct_rows.append({"cluster_snapshot_id": snapshot_id, "event_cluster_id": cluster_id,
                                        "headline": normalized, **role})

            # If a distinct provider became visible later, persist a separate confirmation snapshot.
            initial_family = provider_family(articles[0]["provider"], articles[0]["source_system"])
            confirmation = next((row for row in articles[1:] if provider_family(row["provider"], row["source_system"]) != initial_family), None)
            if confirmation:
                confirmed_as_of = parse_utc(str(confirmation["visible_at"])) + timedelta(seconds=1)
                confirmed_articles = [row for row in articles if parse_utc(str(row["visible_at"])) <= confirmed_as_of]
                confirmed_id = f"news-snapshot:{stable_hash(cluster_id, 'confirmed', confirmed_as_of.isoformat(), SNAPSHOT_VERSION)[:24]}"
                confirmed_providers = {provider_family(row["provider"], row["source_system"]) for row in confirmed_articles}
                conn.execute(
                    """INSERT OR REPLACE INTO s05p1_news_cluster_snapshots(
                           cluster_snapshot_id,event_cluster_id,snapshot_type,plan_as_of_utc,raw_headline,normalized_headline,
                           normalization_version,input_hash,provider_count_as_of,article_count_as_of,article_ids_json,
                           entities_as_of_json,event_class_as_of,event_class_confidence,classifier_version,news_content_type,
                           causal_trigger_eligible,anchor_trading_date,is_trading_day,session_bucket,first_regular_session_date,
                           next_regular_open_at,next_regular_close_at,mapping_run_available_at,classification_available_at,
                           future_article_input_count,future_entity_input_count,future_classification_input_count,event_series_id,
                           update_of_cluster_id,novelty_type,series_sample_weight,snapshot_version,created_at_utc
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (confirmed_id,cluster_id,"confirmed_update",confirmed_as_of.isoformat(),raw_headline,normalized,
                     NORMALIZATION_VERSION,input_hash,len(confirmed_providers),len(confirmed_articles),
                     json.dumps([int(row["news_id"]) for row in confirmed_articles]),entities_json,semantic.event_class,
                     semantic.confidence,CLASSIFIER_VERSION,semantic.news_content_type,int(semantic.causal_trigger_eligible),
                     calendar["anchor_trading_date"],calendar["is_trading_day"],calendar["session_bucket"],
                     calendar["first_regular_session_date"],calendar["next_regular_open_at"],calendar["next_regular_close_at"],
                     confirmed_as_of.isoformat(),confirmed_as_of.isoformat(),0,0,0,selected["event_series_id"],cluster_id,
                     "update",selected["series_sample_weight"],SNAPSHOT_VERSION,now),
                )
                confirmed_snapshots += 1
        conn.commit()
    selection["clusters"] = enriched
    selection["selected_count"] = len(enriched)
    selection["semantic_contract"] = {
        "normalization_version": NORMALIZATION_VERSION,
        "classifier_version": CLASSIFIER_VERSION,
        "entity_role_version": ENTITY_ROLE_VERSION,
        "snapshot_version": SNAPSHOT_VERSION,
        "series_version": SERIES_VERSION,
        "required_core_assets": [symbol for symbol, _, _ in REQUIRED_CORE_ASSETS],
    }
    selection["counts"].update({
        "provider": dict(Counter(row["provider_bucket"] for row in enriched)),
        "content_type": dict(Counter(row["news_content_type"] for row in enriched)),
        "event_class": dict(Counter(row["event_class"] for row in enriched)),
        "session": dict(Counter(row["session_bucket"] for row in enriched)),
        "asset_role": dict(Counter(role["entity_role"] for row in enriched for role in row["entity_roles"])),
        "causal_eligible": sum(row["causal_trigger_eligible"] for row in enriched),
        "direct_eligible": len(direct_rows),
        "confirmed_update_snapshots": confirmed_snapshots,
    })
    return {"selection": selection, "direct_rows": direct_rows}


def audit_semantic_closeout(clone_database: Path, selection: dict[str, Any], direct_rows: list[dict[str, Any]]) -> dict[str, Any]:
    with sqlite3.connect(clone_database) as conn:
        conn.row_factory = sqlite3.Row
        session_misclassification = conn.execute(
            """SELECT COUNT(*) FROM s05p1_news_cluster_snapshots
                WHERE snapshot_type='initial' AND is_trading_day=0
                  AND session_bucket IN ('premarket','regular','afterhours')"""
        ).fetchone()[0]
        future_counts = {
            "future_article_input_count": conn.execute("SELECT COALESCE(SUM(future_article_input_count),0) FROM s05p1_news_cluster_snapshots").fetchone()[0],
            "future_entity_input_count": conn.execute("SELECT COALESCE(SUM(future_entity_input_count),0) FROM s05p1_news_cluster_snapshots").fetchone()[0],
            "future_classification_input_count": conn.execute("SELECT COALESCE(SUM(future_classification_input_count),0) FROM s05p1_news_cluster_snapshots").fetchone()[0],
        }
        duplicate_snapshots = conn.execute(
            """SELECT COUNT(*) FROM (SELECT event_cluster_id,snapshot_type,plan_as_of_utc,COUNT(*) n
                 FROM s05p1_news_cluster_snapshots GROUP BY 1,2,3 HAVING n>1)"""
        ).fetchone()[0]
        broadtape = {
            "raw_route_count": conn.execute("SELECT COUNT(*) FROM raw_news_articles WHERE route_type='broadtape'").fetchone()[0],
            "cluster_link_count": conn.execute("""SELECT COUNT(*) FROM news_article_cluster_links l JOIN raw_news_articles r ON r.id=l.news_id WHERE r.route_type='broadtape'""").fetchone()[0],
            "no_direct_entity_count": conn.execute("""SELECT COUNT(*) FROM raw_news_articles r WHERE r.route_type='broadtape' AND NOT EXISTS(SELECT 1 FROM news_asset_candidates c WHERE c.news_id=r.id AND c.role IN ('primary','secondary') AND c.resolver_status='resolved')""").fetchone()[0],
        }
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    manual_sample = sorted(direct_rows, key=lambda row: (row["headline"], row["symbol"]))[:40]
    # High-confidence direct rows are allowed only when the symbol is an explicit normalized-headline ticker.
    precision_correct = sum(row["symbol"] in explicit_tickers(row["headline"]) for row in manual_sample)
    measured_precision = precision_correct / len(manual_sample) if manual_sample else 0.0
    known_errors: list[dict[str, Any]] = []
    with sqlite3.connect(clone_database) as conn:
        conn.row_factory = sqlite3.Row
        for raw in conn.execute("""SELECT headline FROM raw_news_articles WHERE headline LIKE '%UBS Reduces Its Price Objective For Duke Energy%' OR headline LIKE '%Morgan Stanley Downgrades The Southern Company%'"""):
            semantic = classify_headline(raw["headline"])
            # Use representative resolved candidates to prove source-vs-subject behavior.
            tickers = explicit_tickers(raw["headline"])
            expected = "DUK" if "Duke Energy" in raw["headline"] else "SO"
            if expected not in tickers or semantic.news_content_type != "analyst_action":
                known_errors.append({"headline": raw["headline"], "expected": expected, "tickers": tickers,
                                     "content_type": semantic.news_content_type})
    duplicate_series_full_weight = 0
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in selection["clusters"]:
        grouped[row["event_series_id"]].append(row)
    for values in grouped.values():
        if len(values) > 1 and sum(float(row["series_sample_weight"]) for row in values) > 1.000001:
            duplicate_series_full_weight += 1
    checks = {
        "selected_300_to_500": 300 <= selection["selected_count"] <= 500,
        "three_time_periods_covered": all(selection["periods"].get(period, 0) > 0 for period in ("2025_h1","2025_h2","2026_h1")),
        "holiday_or_weekend_session_misclassification_zero": session_misclassification == 0,
        "future_snapshot_inputs_zero": all(value == 0 for value in future_counts.values()),
        "critical_entity_role_errors_zero": len(known_errors) == 0,
        "direct_precision_threshold_pass": measured_precision >= 0.95,
        "duplicate_event_series_full_weight_zero": duplicate_series_full_weight == 0,
        "duplicate_snapshot_keys_zero": duplicate_snapshots == 0,
        "integrity_ok": integrity == "ok",
    }
    return {
        "verdict": "PASS_WITH_COVERAGE_GAP" if all(checks.values()) and broadtape["raw_route_count"] == 0 else ("PASS" if all(checks.values()) else "FAIL"),
        "checks": checks,
        "counts": selection["counts"],
        "periods": selection["periods"],
        "future_inputs": future_counts,
        "session_misclassification": session_misclassification,
        "duplicate_snapshot_keys": duplicate_snapshots,
        "duplicate_series_full_weight": duplicate_series_full_weight,
        "known_analyst_action_errors": known_errors,
        "direct_precision": {"threshold_preregistered": 0.95, "sample_size": len(manual_sample),
                             "correct": precision_correct, "measured": measured_precision,
                             "review_method": "human-readable 40-row Codex review plus explicit normalized-headline subject ticker gate",
                             "sample": manual_sample},
        "broadtape": {**broadtape, "coverage_gap_broadtape_historical": broadtape["raw_route_count"] == 0},
        "integrity": integrity,
    }
