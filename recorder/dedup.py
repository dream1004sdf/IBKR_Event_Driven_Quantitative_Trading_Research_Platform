from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

from .database import Database
from .models import DedupDecision, NewsInput

_PROVIDER_PREFIX = re.compile(
    r"^(?:update|breaking|briefing\.com|dow jones|reuters|bloomberg)\s*[:\-–—]\s*",
    flags=re.IGNORECASE,
)
_NON_ALNUM = re.compile(r"[^\w\s%$+\-.]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_headline(headline: str) -> str:
    value = html.unescape(headline)
    value = unicodedata.normalize("NFKC", value).lower().strip()
    value = _PROVIDER_PREFIX.sub("", value)
    value = value.replace("’", "'").replace("“", '"').replace("”", '"')
    value = _NON_ALNUM.sub(" ", value)
    value = _WHITESPACE.sub(" ", value).strip(" .-—")
    return value


def headline_digest(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fuzzy_score(left: str, right: str) -> float:
    # token_set catches reordered provider wording; ratio protects against
    # overly broad subset matches. Weighting both reduces false positives.
    return 0.65 * fuzz.token_set_ratio(left, right) + 0.35 * fuzz.ratio(left, right)


@dataclass(frozen=True)
class DedupConfig:
    hash_window_hours: int = 24
    fuzzy_window_minutes: int = 30
    fuzzy_threshold: float = 91.0
    fuzzy_require_same_symbol: bool = True
    minimum_fuzzy_characters: int = 24
    minimum_fuzzy_tokens: int = 4


class NewsDeduplicator:
    def __init__(self, db: Database, config: DedupConfig):
        self.db = db
        self.config = config

    def ingest(self, item: NewsInput) -> DedupDecision:
        exact = self.db.get_news_by_identity(item.provider, item.article_id)
        if exact is not None:
            canonical_id = int(exact["id"])
            self.db.insert_duplicate(
                canonical_id, item, "provider_article_id", 100.0
            )
            symbol_added = self.db.add_news_symbol(
                canonical_id, item.primary_symbol, item.con_id, item.received_at
            )
            return DedupDecision(
                canonical_news_id=canonical_id,
                is_duplicate=True,
                reason="provider_article_id",
                similarity=100.0,
                symbol_added=symbol_added,
            )

        normalized = normalize_headline(item.headline)
        digest = headline_digest(normalized)

        same_hash = self.db.get_recent_news_by_hash(
            digest, item.received_at, self.config.hash_window_hours
        )
        if same_hash is not None:
            canonical_id = int(same_hash["id"])
            self.db.insert_duplicate(
                canonical_id, item, "normalized_headline_hash", 100.0
            )
            symbol_added = self.db.add_news_symbol(
                canonical_id, item.primary_symbol, item.con_id, item.received_at
            )
            return DedupDecision(
                canonical_news_id=canonical_id,
                is_duplicate=True,
                reason="normalized_headline_hash",
                similarity=100.0,
                symbol_added=symbol_added,
            )

        eligible_for_fuzzy = (
            len(normalized) >= self.config.minimum_fuzzy_characters
            and len(normalized.split()) >= self.config.minimum_fuzzy_tokens
        )
        if eligible_for_fuzzy:
            symbol = item.primary_symbol if self.config.fuzzy_require_same_symbol else None
            candidates = self.db.get_recent_headlines(
                item.received_at,
                self.config.fuzzy_window_minutes,
                symbol=symbol,
            )
            best_row = None
            best_score = -1.0
            for row in candidates:
                candidate = str(row["normalized_headline"])
                score = fuzzy_score(normalized, candidate)
                if score > best_score:
                    best_score = score
                    best_row = row

            if best_row is not None and best_score >= self.config.fuzzy_threshold:
                canonical_id = int(best_row["id"])
                self.db.insert_duplicate(
                    canonical_id, item, "fuzzy_time_window", best_score
                )
                symbol_added = self.db.add_news_symbol(
                    canonical_id, item.primary_symbol, item.con_id, item.received_at
                )
                return DedupDecision(
                    canonical_news_id=canonical_id,
                    is_duplicate=True,
                    reason="fuzzy_time_window",
                    similarity=best_score,
                    symbol_added=symbol_added,
                )

        news_id = self.db.insert_news(item, normalized, digest)
        symbol_added = self.db.add_news_symbol(
            news_id, item.primary_symbol, item.con_id, item.received_at
        )
        return DedupDecision(
            canonical_news_id=news_id,
            is_duplicate=False,
            symbol_added=symbol_added,
        )
