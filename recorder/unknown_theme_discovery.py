from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .analysis_database import AnalysisDatabase
from .ai_provider import PermanentAnalysisError, TransientAnalysisError
from .database import utc_iso
from .news_asset_mapper import infer_news_assets

UTC = timezone.utc
UNKNOWN_THEME_DISCOVERY_VERSION = "unknown-theme-discovery-v1"

_STOPWORDS = {
    "about",
    "after",
    "ahead",
    "amid",
    "announces",
    "around",
    "before",
    "broker",
    "coverage",
    "downgrades",
    "from",
    "group",
    "higher",
    "initiates",
    "maintains",
    "market",
    "markets",
    "price",
    "raises",
    "rating",
    "says",
    "shares",
    "stock",
    "stocks",
    "target",
    "today",
    "trading",
    "with",
}


class UnknownThemeCandidateReview(BaseModel):
    candidate_key: str
    verdict: Literal["promote_candidate", "monitor_only", "routine_data", "already_covered", "noise"]
    proposed_theme: str = ""
    proposed_asset_map: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""
    threshold_hint: str = ""


class UnknownThemeReviewBatch(BaseModel):
    review_version: str = "unknown-theme-ai-review-v1"
    summary: str = ""
    reviews: list[UnknownThemeCandidateReview]


@dataclass(frozen=True)
class UnknownThemeDiscoveryConfig:
    database_path: Path
    lookback_minutes: int = 240
    min_group_events: int = 1
    limit: int = 500
    include_medium: bool = False
    ai_review: bool = False
    openai_model: str = "gpt-5.4-mini"
    reasoning_effort: str = "low"
    max_ai_groups: int = 20
    output_path: Path | None = None
    json_output_path: Path | None = None


def _tokens(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z][A-Za-z0-9.\-]{2,}", text.lower())
    result: list[str] = []
    for token in raw:
        cleaned = token.strip(".-").replace("&#x2014", "")
        if len(cleaned) < 3 or cleaned in _STOPWORDS:
            continue
        if cleaned.isdigit():
            continue
        if cleaned not in result:
            result.append(cleaned)
    return result[:16]


def _candidate_key(tokens: list[str], mapping: dict[str, Any]) -> str:
    themes = [str(item).lower() for item in mapping.get("themes") or [] if str(item).strip()]
    if themes:
        return "theme:" + "|".join(themes[:2])
    useful = [token for token in tokens if token not in _STOPWORDS]
    return "tokens:" + "|".join(useful[:3]) if useful else "tokens:unmapped"


def _should_queue(mapping: dict[str, Any], *, include_medium: bool) -> bool:
    confidence = str(mapping.get("confidence") or "").lower()
    if confidence == "low":
        return True
    if include_medium and confidence == "medium":
        return True
    candidates = [str(item).upper() for item in mapping.get("target_candidates") or []]
    return candidates in (["SPY"], ["SPY", "QQQ"]) and confidence != "high"


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value if value is not None else default


def _collect_rows(db: AnalysisDatabase, since: str, limit: int) -> list[dict[str, Any]]:
    with db.connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, provider, headline, body, primary_symbol, published_at, received_at, extra_data
                FROM news_articles
                WHERE COALESCE(published_at, received_at, created_at) >= ?
                ORDER BY COALESCE(published_at, received_at, created_at) DESC, id DESC
                LIMIT ?
                """,
                (since, max(1, int(limit))),
            )
        ]


def _upsert_candidate(db: AnalysisDatabase, item: dict[str, Any]) -> bool:
    now = utc_iso(datetime.now(UTC))
    before = 0
    with db.connect() as conn:
        before = conn.total_changes
        conn.execute(
            """
            INSERT INTO unknown_theme_candidates(
                event_source,source_id,candidate_key,observed_at,provider,primary_symbol,headline,
                mapping_confidence,mapping_reason,source_mode,tokens_json,inferred_mapping_json,
                provisional_asset_map_json,market_reaction_json,ai_review_json,status,discovery_version,
                created_at,updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(event_source,source_id,discovery_version) DO UPDATE SET
                candidate_key=excluded.candidate_key,
                mapping_confidence=excluded.mapping_confidence,
                mapping_reason=excluded.mapping_reason,
                source_mode=excluded.source_mode,
                tokens_json=excluded.tokens_json,
                inferred_mapping_json=excluded.inferred_mapping_json,
                provisional_asset_map_json=excluded.provisional_asset_map_json,
                updated_at=excluded.updated_at
            """,
            (
                item["event_source"],
                item["source_id"],
                item["candidate_key"],
                item["observed_at"],
                item["provider"],
                item["primary_symbol"],
                item["headline"],
                item["mapping_confidence"],
                item["mapping_reason"],
                item["source_mode"],
                json.dumps(item["tokens"], ensure_ascii=False),
                json.dumps(item["inferred_mapping"], ensure_ascii=False, sort_keys=True),
                json.dumps(item["provisional_asset_map"], ensure_ascii=False),
                "{}",
                "{}",
                "queued",
                UNKNOWN_THEME_DISCOVERY_VERSION,
                now,
                now,
            ),
        )
        return conn.total_changes > before


def _groups(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in candidates:
        grouped[item["candidate_key"]].append(item)
    output: list[dict[str, Any]] = []
    for key, items in grouped.items():
        token_counts: Counter[str] = Counter()
        asset_counts: Counter[str] = Counter()
        providers: Counter[str] = Counter()
        for item in items:
            token_counts.update(item.get("tokens") or [])
            asset_counts.update(item.get("provisional_asset_map") or [])
            providers.update([str(item.get("provider") or "")])
        latest = sorted(items, key=lambda row: str(row.get("observed_at") or ""), reverse=True)[:5]
        output.append(
            {
                "candidate_key": key,
                "event_count": len(items),
                "top_tokens": [token for token, _ in token_counts.most_common(10)],
                "provisional_asset_map": [asset for asset, _ in asset_counts.most_common(12)],
                "provider_counts": dict(providers),
                "latest_headlines": [
                    {
                        "source_id": row.get("source_id"),
                        "observed_at": row.get("observed_at"),
                        "headline": row.get("headline"),
                        "mapping_confidence": row.get("mapping_confidence"),
                        "mapping_reason": row.get("mapping_reason"),
                    }
                    for row in latest
                ],
            }
        )
    output.sort(key=lambda row: (int(row["event_count"]), len(row["top_tokens"])), reverse=True)
    return output


def _extract_ai_review(response: Any) -> UnknownThemeReviewBatch:
    for item in getattr(response, "output", []) or []:
        for content in getattr(item, "content", []) or []:
            parsed = getattr(content, "parsed", None)
            if isinstance(parsed, UnknownThemeReviewBatch):
                return parsed
    raise TransientAnalysisError("Structured response did not contain UnknownThemeReviewBatch")


def _map_openai_exception(exc: Exception) -> Exception:
    name = exc.__class__.__name__
    message = str(exc)[:1500]
    status = getattr(exc, "status_code", None)
    if name in {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"} or (
        isinstance(status, int) and status >= 500
    ):
        return TransientAnalysisError(f"OpenAI transient unknown-theme review error ({name}): {message}")
    if name == "AuthenticationError":
        return PermanentAnalysisError("OpenAI unknown-theme review authentication failed. Check OPENAI_API_KEY.")
    if name in {"PermissionDeniedError", "NotFoundError", "UnprocessableEntityError", "BadRequestError"} or (
        isinstance(status, int) and 400 <= status < 500
    ):
        return PermanentAnalysisError(f"OpenAI unknown-theme review request/configuration error ({name}): {message}")
    return TransientAnalysisError(f"OpenAI unexpected unknown-theme review error ({name}): {message}")


def review_unknown_theme_groups_with_openai(
    groups: list[dict[str, Any]],
    *,
    api_key: str | None,
    model: str,
    reasoning_effort: str,
    max_groups: int,
) -> dict[str, Any]:
    if not api_key:
        raise PermanentAnalysisError("OPENAI_API_KEY is missing for unknown-theme review.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise PermanentAnalysisError("The openai package is not installed. Run pip install -r requirements.txt.") from exc
    compact = groups[: max(0, int(max_groups))]
    payload = {
        "task": (
            "Review low-confidence market-news theme candidates. Decide whether each group is a new tradable/news "
            "theme worth monitoring, routine data/noise, already covered by existing regime logic, or promotable "
            "after validation. Draft a provisional asset map. This is not trading advice."
        ),
        "verdicts": ["promote_candidate", "monitor_only", "routine_data", "already_covered", "noise"],
        "groups": compact,
    }
    try:
        client = OpenAI(api_key=api_key, timeout=90.0, max_retries=0)
        response = client.responses.parse(
            model=model,
            input=[
                {
                    "role": "system",
                    "content": (
                        "You are a skeptical market theme discovery analyst. "
                        "Classify new/unknown news themes and draft provisional asset maps only when justified."
                    ),
                },
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)[:60000]},
            ],
            text_format=UnknownThemeReviewBatch,
            reasoning={"effort": reasoning_effort},
            store=False,
        )
        parsed = _extract_ai_review(response)
    except (PermanentAnalysisError, TransientAnalysisError):
        raise
    except (ValidationError, Exception) as exc:
        mapped = _map_openai_exception(exc)
        raise mapped from exc
    return parsed.model_dump(mode="json")


def _store_ai_reviews(db: AnalysisDatabase, review_batch: dict[str, Any]) -> None:
    reviews = {str(item.get("candidate_key") or ""): item for item in review_batch.get("reviews") or []}
    now = utc_iso(datetime.now(UTC))
    with db.connect() as conn:
        for key, review in reviews.items():
            status = "reviewed"
            verdict = str(review.get("verdict") or "")
            if verdict == "noise":
                status = "rejected"
            elif verdict in {"promote_candidate", "monitor_only"}:
                status = "provisional"
            conn.execute(
                """
                UPDATE unknown_theme_candidates
                SET ai_review_json=?, status=?, updated_at=?
                WHERE candidate_key=? AND discovery_version=?
                """,
                (
                    json.dumps(review, ensure_ascii=False, sort_keys=True),
                    status,
                    now,
                    key,
                    UNKNOWN_THEME_DISCOVERY_VERSION,
                ),
            )


def discover_unknown_theme_candidates(
    config: UnknownThemeDiscoveryConfig,
    *,
    openai_api_key: str | None = None,
) -> dict[str, Any]:
    db = AnalysisDatabase(config.database_path)
    now = datetime.now(UTC)
    since = (now - timedelta(minutes=max(1, int(config.lookback_minutes)))).isoformat()
    scanned = _collect_rows(db, since, config.limit)
    queued: list[dict[str, Any]] = []
    created_or_updated = 0
    for row in scanned:
        mapping = infer_news_assets(
            str(row.get("headline") or ""),
            str(row.get("body") or ""),
            primary_symbol=str(row.get("primary_symbol") or ""),
            provider=str(row.get("provider") or ""),
            extra_data=str(row.get("extra_data") or ""),
        ).as_dict()
        if not _should_queue(mapping, include_medium=config.include_medium):
            continue
        tokens = _tokens(str(row.get("headline") or ""))
        assets = list(dict.fromkeys(mapping.get("target_candidates") or mapping.get("inferred_assets") or ["SPY"]))
        item = {
            "event_source": "news_articles",
            "source_id": int(row["id"]),
            "candidate_key": _candidate_key(tokens, mapping),
            "observed_at": str(row.get("published_at") or row.get("received_at") or ""),
            "provider": str(row.get("provider") or ""),
            "primary_symbol": str(row.get("primary_symbol") or ""),
            "headline": str(row.get("headline") or ""),
            "mapping_confidence": str(mapping.get("confidence") or ""),
            "mapping_reason": str(mapping.get("reason") or ""),
            "source_mode": str(mapping.get("source_mode") or ""),
            "tokens": tokens,
            "inferred_mapping": mapping,
            "provisional_asset_map": assets,
        }
        if _upsert_candidate(db, item):
            created_or_updated += 1
        queued.append(item)
    groups = [group for group in _groups(queued) if int(group["event_count"]) >= max(1, int(config.min_group_events))]
    ai_review: dict[str, Any] = {}
    ai_error = ""
    if config.ai_review and groups:
        try:
            ai_review = review_unknown_theme_groups_with_openai(
                groups,
                api_key=openai_api_key,
                model=config.openai_model,
                reasoning_effort=config.reasoning_effort,
                max_groups=config.max_ai_groups,
            )
            _store_ai_reviews(db, ai_review)
        except Exception as exc:
            ai_error = str(exc)[:1500]
    result = {
        "version": UNKNOWN_THEME_DISCOVERY_VERSION,
        "generated_at": now.isoformat(),
        "lookback_minutes": config.lookback_minutes,
        "scanned_news_rows": len(scanned),
        "queued_candidate_rows": len(queued),
        "created_or_updated_rows": created_or_updated,
        "group_count": len(groups),
        "groups": groups,
        "ai_review": ai_review,
        "ai_error": ai_error,
        "safety": {
            "orders_submitted": False,
            "transmit_enabled": False,
            "live_scorer_mutated": False,
            "mapper_rules_promoted": False,
        },
    }
    if config.json_output_path:
        write_unknown_theme_discovery_json(config.json_output_path, result)
    if config.output_path:
        write_unknown_theme_discovery_markdown(config.output_path, result)
    return result


def unknown_theme_discovery_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Unknown Theme Discovery",
        "",
        f"- Generated: {result.get('generated_at')}",
        f"- Scanned news rows: {result.get('scanned_news_rows')}",
        f"- Queued candidate rows: {result.get('queued_candidate_rows')}",
        f"- Groups: {result.get('group_count')}",
        f"- AI review: {'yes' if result.get('ai_review') else 'no'}",
    ]
    if result.get("ai_error"):
        lines.append(f"- AI error: {result.get('ai_error')}")
    lines.extend(["", "## Candidate groups", ""])
    groups = result.get("groups") or []
    if not groups:
        lines.append("- none")
    for group in groups[:30]:
        lines.append(
            f"### {group.get('candidate_key')} ({group.get('event_count')} events)"
        )
        lines.append(f"- Tokens: {', '.join(group.get('top_tokens') or []) or '-'}")
        lines.append(f"- Provisional assets: {', '.join(group.get('provisional_asset_map') or []) or '-'}")
        lines.append(f"- Providers: `{group.get('provider_counts') or {}}`")
        for headline in (group.get("latest_headlines") or [])[:3]:
            lines.append(f"  - {headline.get('observed_at')} {headline.get('headline')}")
        lines.append("")
    review = result.get("ai_review") or {}
    if review:
        lines.extend(["## AI review", "", f"- Summary: {review.get('summary')}", ""])
        for item in review.get("reviews") or []:
            lines.append(
                f"- `{item.get('candidate_key')}` → {item.get('verdict')} / {item.get('proposed_theme')} "
                f"assets={item.get('proposed_asset_map')} confidence={item.get('confidence')}"
            )
    return "\n".join(lines).rstrip() + "\n"


def write_unknown_theme_discovery_json(path: Path, result: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_unknown_theme_discovery_markdown(path: Path, result: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unknown_theme_discovery_markdown(result), encoding="utf-8")
    return path
