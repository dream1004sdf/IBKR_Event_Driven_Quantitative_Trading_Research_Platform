from recorder.news_asset_mapper import infer_news_assets, preferred_trading_symbol


def test_broadtape_mapper_extracts_company_and_market_proxies():
    mapping = infer_news_assets(
        "Stock Market Today: Dow Jones Futures Rise, Nasdaq Falls as Trump Says Iran Talks Continue; CoreWeave, Nebius, Micron in Focus",
        primary_symbol="BROADTAPE:BZ",
        provider="BZ",
    )

    assert {"CRWV", "NBIS", "MU"}.issubset(set(mapping.inferred_symbols))
    assert "SPY" in mapping.target_candidates
    assert "QQQ" in mapping.target_candidates
    assert mapping.confidence in {"high", "medium"}


def test_broadtape_mapper_prefers_energy_proxy_for_oil_equity_headline():
    mapping = infer_news_assets(
        "Jefferies Maintains Buy on EOG Resources, Raises Price Target to $175",
        primary_symbol="BROADTAPE:BZ",
        provider="BZ",
    )
    symbol, confidence, source = preferred_trading_symbol(mapping)

    assert "EOG" in mapping.inferred_symbols
    assert symbol == "XLE"
    assert confidence in {"high", "medium"}
    assert source == "broadtape_asset_mapper"


def test_broadtape_no_entity_uses_spy_only_as_context():
    mapping = infer_news_assets(
        "Company Announces Strategic Review",
        primary_symbol="BROADTAPE:BZ",
        provider="BZ",
    )

    assert mapping.target_candidates == ()
    assert mapping.inferred_assets == ()
    assert mapping.context_assets == ("SPY",)
    assert mapping.tradable_candidate is False
    assert mapping.fallback_allowed_for_context is True
