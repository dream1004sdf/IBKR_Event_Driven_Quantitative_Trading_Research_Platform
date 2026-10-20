from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from recorder.attribution import attribute_event_targets
from recorder.database import Database
from recorder.models import NewsInput, QuoteSnapshot
from recorder.price_cache import PriceCache
from recorder.prompting import build_input_snapshot
from recorder.quote_integrity import QuoteIntegrityConfig, assess_quote_integrity
from recorder.reactions import ReactionTracker

UTC = timezone.utc


def _baseline(symbol: str, price: float, *, eligible: int = 1, status: str = "valid") -> dict:
    return {
        "symbol": symbol,
        "captured_at": datetime.now(UTC).isoformat(),
        "bid": price - 0.1,
        "ask": price + 0.1,
        "last": price,
        "midpoint": price,
        "selected_price": price if eligible else None,
        "selected_price_source": "last" if eligible else None,
        "volume": 1000,
        "currency": "USD",
        "con_id": 123,
        "contract_symbol": symbol,
        "sec_type": "STK",
        "exchange": "SMART",
        "primary_exchange": "NASDAQ",
        "multiplier": "1",
        "market_data_type": 1,
        "quote_integrity_status": status,
        "quote_integrity_reasons_json": "[]",
        "reaction_baseline_eligible": eligible,
        "spread_ratio": 0.002,
        "last_midpoint_deviation": 0.0,
        "close_reference_deviation": 0.01,
    }


def test_wide_spread_quote_fails_and_cannot_be_selected():
    at = datetime.now(UTC)
    quote = QuoteSnapshot(
        symbol="MU",
        timestamp=at,
        bid=100.0,
        ask=120.0,
        last=110.0,
        close=109.0,
        currency="USD",
        con_id=123,
        contract_symbol="MU",
        sec_type="STK",
        exchange="SMART",
        primary_exchange="NASDAQ",
        multiplier="1",
        market_data_type=1,
    )
    result = assess_quote_integrity(
        quote,
        reference_time=at,
        config=QuoteIntegrityConfig(max_spread_ratio=0.05),
    )
    assert result.status == "failed"
    assert result.eligible is False
    assert result.selected_price is None
    assert any("spread ratio" in reason for reason in result.reasons)


def test_contract_symbol_mismatch_fails_quote():
    at = datetime.now(UTC)
    quote = QuoteSnapshot(
        symbol="MU",
        timestamp=at,
        last=100.0,
        currency="USD",
        con_id=123,
        contract_symbol="NVDA",
        sec_type="STK",
        exchange="SMART",
        multiplier="1",
    )
    result = assess_quote_integrity(quote, reference_time=at)
    assert result.status == "failed"
    assert any("contract symbol mismatch" in reason for reason in result.reasons)


def test_reaction_tracker_preserves_failed_quote_but_creates_no_targets(tmp_path: Path):
    db = Database(tmp_path / "quotes.db")
    at = datetime.now(UTC)
    headline = "Micron announces a new product"
    news_id = db.insert_news(
        NewsInput("TEST", "q1", headline, at, at, "MU", 123, ""),
        headline.lower(),
        "quote-hash",
    )
    cache = PriceCache()
    cache.put_snapshot(
        QuoteSnapshot(
            symbol="MU",
            timestamp=at,
            bid=100.0,
            ask=130.0,
            last=115.0,
            currency="USD",
            con_id=123,
            contract_symbol="MU",
            sec_type="STK",
            exchange="SMART",
            multiplier="1",
        )
    )
    tracker = ReactionTracker(
        db,
        cache,
        ("15m", "1h"),
        max_spread_ratio=0.05,
    )
    tracker.register_event(news_id, at, ("MU",))
    with db.connect() as conn:
        baseline = conn.execute(
            "SELECT * FROM market_baselines WHERE news_id=? AND symbol='MU'", (news_id,)
        ).fetchone()
        targets = conn.execute(
            "SELECT COUNT(*) FROM reaction_targets WHERE news_id=?", (news_id,)
        ).fetchone()[0]
    assert baseline is not None
    assert baseline["quote_integrity_status"] == "failed"
    assert baseline["selected_price"] is None
    assert baseline["reaction_baseline_eligible"] == 0
    assert targets == 0


def test_mismatch_snapshot_separates_delivery_quote_from_target_quote():
    at = datetime.now(UTC)
    news = {
        "id": 1,
        "provider": "DJ-N",
        "article_id": "bb",
        "published_at": at.isoformat(),
        "received_at": at.isoformat(),
        "primary_symbol": "NVDA",
        "headline": "BlackBerry Shares Jump 23% as an AI Play",
        "body": "BlackBerry shares rose after an AI software update.",
    }
    attribution = attribute_event_targets(
        headline=news["headline"],
        body=news["body"],
        original_symbol="NVDA",
        available_symbols=("NVDA", "SPY", "QQQ"),
    )
    snapshot = build_input_snapshot(
        news=news,
        linked_symbols=["NVDA", "SPY", "QQQ"],
        baselines=[_baseline("NVDA", 200.0), _baseline("SPY", 700.0)],
        required_horizons=("15m", "1h"),
        max_body_chars=12000,
        include_article_body=True,
        attribution=attribution,
    )
    assert attribution.attribution_status == "mismatch"
    assert snapshot["baseline_market_snapshot"] == []
    assert snapshot["target_quote_audit_snapshot"] == []
    assert snapshot["delivery_market_snapshot"][0]["symbol"] == "NVDA"
    assert snapshot["delivery_market_snapshot"][0]["selected_price"] == 200.0


def test_broad_market_article_uses_market_scope_and_keeps_mu_as_mention():
    decision = attribute_event_targets(
        headline="Dow Jones Futures Fall, Techs Skid Amid Mixed Market Signals; What To Do Now",
        body="Micron closed higher by 15.7%. Nvidia fell and the Nasdaq declined.",
        original_symbol="MU",
        available_symbols=("MU", "SPY", "QQQ", "IWM", "TLT"),
    )
    assert decision.event_scope == "market"
    assert decision.article_scope == "market"
    assert decision.article_primary_subject == "us_equities"
    assert decision.primary_symbol == "SPY"
    assert "MU" in decision.mentioned_symbols
    assert decision.subscription_entity_role == "mentioned_entity"
    assert decision.attribution_status == "delivery_proxy"


def test_target_quote_audit_retains_failed_row_but_model_reference_excludes_it():
    at = datetime.now(UTC)
    news = {
        "id": 2,
        "provider": "DJ-N",
        "article_id": "mu",
        "published_at": at.isoformat(),
        "received_at": at.isoformat(),
        "primary_symbol": "MU",
        "headline": "Micron price target raised",
        "body": "Micron price target was raised.",
    }
    attribution = attribute_event_targets(
        headline=news["headline"],
        body=news["body"],
        original_symbol="MU",
        available_symbols=("MU", "SPY"),
    )
    failed = _baseline("MU", 100.0, eligible=0, status="failed")
    snapshot = build_input_snapshot(
        news=news,
        linked_symbols=["MU", "SPY"],
        baselines=[failed],
        required_horizons=("15m",),
        max_body_chars=12000,
        include_article_body=True,
        attribution=attribution,
    )
    assert snapshot["baseline_market_snapshot"] == []
    assert snapshot["target_quote_audit_snapshot"][0]["quote_integrity_status"] == "failed"


def test_contract_metadata_round_trip_in_price_cache():
    cache = PriceCache()
    cache.set_contract_metadata(
        "MU",
        con_id=12345,
        contract_symbol="MU",
        sec_type="STK",
        exchange="SMART",
        primary_exchange="NASDAQ",
        currency="USD",
        multiplier="1",
        market_data_type=1,
    )
    cache.update_price("MU", "last", 100.0)
    quote = cache.get("MU")
    assert quote is not None
    assert quote.con_id == 12345
    assert quote.contract_symbol == "MU"
    assert quote.sec_type == "STK"
    assert quote.primary_exchange == "NASDAQ"
    assert quote.market_data_type == 1
