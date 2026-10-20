from __future__ import annotations

import pytest

from recorder.s05_proxy_semantic_v4 import (
    classify_v4_causal_proxy_headline,
    infer_channel_v4,
    infer_primary_proxy_v4,
    infer_region_v4,
    match_proxy_rule_v4,
)


@pytest.mark.parametrize(
    ("headline", "reason"),
    (
        ("Reported Earlier, Japan Interest Rate Decision Unchanged At 0.50%", "duplicate_or_repeat"),
        ("The Tickers To Watch Ahead Of Russia-Ukraine Ceasefire Talks", "listicle"),
        ("Stock Futures Surge, Oil Tumbles On Iran Ceasefire Optimism", "market_recap"),
        ("Oil Prices Spike To $98, Stocks Dip After Israeli Attack", "price_move_recap"),
        ("OPEC+ May Consider Larger Oil Output Hike On Sunday", "preview"),
        ("Cramer Warns Of Overconfident Market After Ceasefire", "opinion_or_analysis"),
        ("OPEC Cuts 2026 Global Oil-Demand Growth Forecast", "opinion_or_analysis"),
        ("Trump Approval Rating Drops After Attack On Iran", "non_material_commentary"),
        ("RTX Lands $1.7 Billion Missile Defense Contract", "wrong_proxy_family"),
        ("AT&T Data Breach Settlement: Here's How To Claim", "listicle"),
    ),
)
def test_v4_rejects_generalized_set_a_false_positive_classes(headline: str, reason: str) -> None:
    result = classify_v4_causal_proxy_headline(headline)
    assert result.eligible is False
    assert result.exclusion_reason == reason


@pytest.mark.parametrize(
    "headline",
    (
        "FOMC Raises Interest Rates By 25 Basis Points",
        "Fed's Logan Says No Further Rate Cuts Will Be Needed If Inflation Falls",
        "Iran Launches Missile Barrage Following Israeli Strikes",
        "OPEC+ Members Agree To Raise Oil Output By 137K Barrels/Day",
        "Iran Seizes Two Vessels In Strait Of Hormuz",
        "US Imposes Sanctions On Iranian Oil Exports",
        "Company Confirms Ransomware Attack Compromised Customer Accounts",
    ),
)
def test_v4_keeps_explicit_actions_and_authoritative_updates(headline: str) -> None:
    assert classify_v4_causal_proxy_headline(headline).eligible is True


def test_v4_family_precedence_uses_fed_policy_over_incidental_oil() -> None:
    headline = "Fed's Daly Says Oil Shock May Delay Rate Cuts As Policy Remains Restrictive"
    rule = match_proxy_rule_v4(headline)
    assert rule is not None
    assert rule.family == "rates_policy"


def test_v4_family_precedence_uses_oil_supply_for_hormuz_transit_action() -> None:
    headline = "Iran Reopens Strait Of Hormuz For Commercial Oil Tankers After Ceasefire"
    rule = match_proxy_rule_v4(headline)
    assert rule is not None
    assert rule.family == "oil_supply"


@pytest.mark.parametrize(
    "headline",
    (
        "Iran Is Unwilling To Talk About Opening Hormuz While Under Attack",
        "White House Says Pentagon And Energy Department Are Working On A Plan To Secure Strait Of Hormuz",
        "Iran Agrees To The Complete And Safe Opening Of The Strait Of Hormuz",
    ),
)
def test_v4_hormuz_opening_or_security_action_uses_oil_supply(headline: str) -> None:
    rule = match_proxy_rule_v4(headline)
    assert rule is not None
    assert rule.family == "oil_supply"


def test_v4_does_not_reclassify_conditional_hormuz_diplomacy_as_oil_supply() -> None:
    headline = "Pakistan Asks Iran To Open Strait Of Hormuz If Ceasefire Talks Succeed"
    rule = match_proxy_rule_v4(headline)
    assert rule is not None
    assert rule.family == "defense_conflict"


def test_v4_defense_company_contract_has_no_geopolitical_proxy() -> None:
    assert match_proxy_rule_v4("Lockheed Martin Lands A $180 Million Defense Contract") is None


def test_v4_mapping_uses_affected_subject_not_incidental_first_country() -> None:
    fed = "Fed's Daly Says Iran Conflict Could Delay A U.S. Rate Cut"
    assert infer_region_v4("rates_policy", fed) == "united_states"
    assert infer_channel_v4("rates_policy", fed) == "sovereign_risk"
    assert infer_primary_proxy_v4("rates_policy", "united_states", "sovereign_risk") == "IEF"

    opec = "OPEC+ Says Russia, Iraq And Kazakhstan Submit Oil Compensation Cuts"
    assert infer_region_v4("oil_supply", opec) == "global"
    assert infer_channel_v4("oil_supply", opec) == "energy"
    assert infer_primary_proxy_v4("oil_supply", "global", "energy") == "XLE"

    fed_with_oil = "Fed's Miran Says Policy Is Too Tight And The Fed Does Not Respond To Oil Prices"
    assert infer_channel_v4("rates_policy", fed_with_oil) == "sovereign_risk"


def test_v4_region_uses_policy_or_affected_subject_precedence() -> None:
    assert infer_region_v4(
        "oil_supply",
        "White House Will Not Implement A Crude Export Ban, Administration Official Says",
    ) == "united_states"
    assert infer_region_v4(
        "defense_conflict",
        "Hamas Holds Talks To Safeguard The Gaza Ceasefire Under Strain From The Iran War",
    ) == "middle_east"
    assert infer_region_v4(
        "oil_supply",
        "Kazakhstan Energy Minister Says Oil Exports Continue Despite Prior Ukrainian Drone Attacks",
    ) == "kazakhstan"
    assert infer_region_v4(
        "korea_risk",
        "Russia And China Call For Reduced Military Pressure On North Korea And Discuss Taiwan",
    ) == "korea"


def test_v4_rejects_ambiguous_multi_event_compound_headline() -> None:
    result = classify_v4_causal_proxy_headline(
        "Middle East Conflict Escalates As Israel Demands A Strike Window On Iranian Energy; "
        "China's Oil Imports Collapse"
    )
    assert result.eligible is False
    assert result.matched_rule == "ambiguous_multi_event_compound"


def test_v4_preserves_existing_conflict_region_when_family_is_unchanged() -> None:
    headline = "Israel And Iran Agree To A Ceasefire Covering Lebanon"
    assert infer_region_v4("defense_conflict", headline) == "iran"
