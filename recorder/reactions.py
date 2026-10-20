from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .calendar_utils import build_schedule
from .database import Database, parse_utc
from .price_cache import PriceCache
from .quote_integrity import QuoteIntegrityConfig, assess_quote_integrity

UTC = timezone.utc


class ReactionTracker:
    def __init__(
        self,
        db: Database,
        prices: PriceCache,
        horizons: tuple[str, ...],
        max_price_age_seconds: int = 180,
        max_lateness_seconds: int = 180,
        max_attempts: int = 5,
        retry_base_seconds: int = 5,
        retry_max_seconds: int = 60,
        *,
        quote_integrity_enabled: bool = True,
        max_spread_ratio: float = 0.05,
        max_last_midpoint_deviation: float = 0.10,
        max_close_reference_deviation: float = 0.75,
        require_stock_contract: bool = True,
    ):
        self.db = db
        self.prices = prices
        self.horizons = horizons
        self.max_price_age_seconds = max_price_age_seconds
        self.max_lateness_seconds = max_lateness_seconds
        self.max_attempts = max_attempts
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.quote_integrity_config = QuoteIntegrityConfig(
            enabled=quote_integrity_enabled,
            max_age_seconds=max_price_age_seconds,
            max_spread_ratio=max_spread_ratio,
            max_last_midpoint_deviation=max_last_midpoint_deviation,
            max_close_reference_deviation=max_close_reference_deviation,
            require_stock_contract=require_stock_contract,
        )
        self.log = logging.getLogger(self.__class__.__name__)

    def register_event(
        self, news_id: int, event_time: datetime, symbols: tuple[str, ...]
    ) -> None:
        schedule = build_schedule(event_time, self.horizons)
        for symbol in symbols:
            quote = self.prices.get(symbol)
            baseline_eligible = False
            if quote is not None:
                integrity = assess_quote_integrity(
                    quote,
                    reference_time=event_time,
                    config=self.quote_integrity_config,
                )
                baseline_eligible = self.db.insert_baseline(news_id, quote, integrity)
                if not baseline_eligible:
                    self.log.warning(
                        "Quote rejected as reaction baseline for %s: %s",
                        symbol,
                        "; ".join(integrity.reasons),
                    )
            else:
                self.log.warning("No event-time quote available for %s", symbol)

            if not baseline_eligible:
                continue
            for horizon, due_at in schedule.items():
                self.db.create_reaction_target(news_id, symbol, horizon, due_at)

    def _retry(self, target_id: int, attempts: int, message: str, now: datetime) -> None:
        delay = min(self.retry_max_seconds, self.retry_base_seconds * (2 ** max(0, attempts)))
        self.db.schedule_target_retry(
            target_id, message, retry_at=now + timedelta(seconds=delay),
            max_attempts=self.max_attempts,
        )

    def process_due(self, now: datetime | None = None, limit: int = 100) -> int:
        now = now or datetime.now(UTC)
        completed = 0
        for target in self.db.get_due_targets(now, limit=limit):
            target_id = int(target["id"])
            symbol = str(target["symbol"])
            due_at = parse_utc(str(target["due_at"]))
            lateness = (now - due_at).total_seconds()
            if lateness > self.max_lateness_seconds:
                self.db.mark_target_terminal(
                    target_id, "missed",
                    f"historical backfill required; target overdue by {lateness:.1f} seconds",
                )
                continue
            baseline = target["baseline_price"]
            if baseline is None or float(baseline) <= 0:
                self.db.mark_target_terminal(target_id, "failed", "eligible baseline price unavailable")
                continue

            quote = self.prices.get(symbol)
            if quote is None:
                self._retry(target_id, int(target["attempts"]), "current quote unavailable", now)
                continue

            integrity = assess_quote_integrity(
                quote,
                reference_time=now,
                config=self.quote_integrity_config,
            )
            if not integrity.eligible:
                self._retry(
                    target_id, int(target["attempts"]),
                    "reaction quote integrity failed: " + "; ".join(integrity.reasons), now,
                )
                continue
            price = integrity.selected_price
            source = integrity.selected_source
            selected_at = integrity.selected_at
            if price is None or source is None or selected_at is None:
                self._retry(target_id, int(target["attempts"]), "usable reaction price unavailable", now)
                continue

            self.db.complete_target(
                target_id=target_id,
                news_id=int(target["news_id"]),
                symbol=symbol,
                horizon=str(target["horizon"]),
                due_at=due_at,
                captured_at=selected_at,
                baseline_price=float(baseline),
                reaction_price=float(price),
                price_source=source,
            )
            completed += 1
        return completed
