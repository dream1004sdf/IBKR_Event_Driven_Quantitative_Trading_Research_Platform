from __future__ import annotations

from recorder.s05_proxy_semantic_closeout import (
    classification_compatible,
    infer_channel,
    infer_direction,
    has_s05p3_outcome_or_context,
    infer_primary_proxy,
    infer_region,
)


def test_region_specific_rate_and_china_taiwan_proxies() -> None:
    japan = "Reported Earlier, Japan Interest Rate Decision Unchanged At 0.50%"
    assert infer_region("rates_policy", japan) == "japan"
    assert infer_primary_proxy("rates_policy", "japan", "sovereign_risk", japan) == "EWJ"

    taiwan = "Taiwan Imposes Export Controls On China's Huawei and SMIC"
    assert infer_region("china_taiwan", taiwan) == "taiwan"
    assert infer_primary_proxy("china_taiwan", "taiwan", "technology", taiwan) == "EWT"

    china = "US Freezes Export Controls To Secure Trade Deal With China"
    assert infer_region("china_taiwan", china) == "china"
    assert infer_primary_proxy("china_taiwan", "china", "technology", china) == "FXI"


def test_sanctions_proxy_depends_on_affected_channel() -> None:
    energy = "US Imposes Sanctions On Iranian Oil Exports"
    channel = infer_channel("sanctions", energy)
    assert channel == "energy"
    assert infer_primary_proxy("sanctions", "iran", channel, energy) == "XLE"

    tech = "US Plans Wider China Tech Sanctions With Subsidiary Crackdown"
    channel = infer_channel("sanctions", tech)
    assert channel == "technology"
    assert infer_primary_proxy("sanctions", "china", channel, tech) == "FXI"


def test_direction_and_classification_compatibility_are_explicit() -> None:
    assert infer_direction("sanctions", "China Lifts Sanctions On European Lawmakers") == "easing"
    assert infer_direction("defense_conflict", "Israel And Hamas Sign Ceasefire") == "deescalation"
    assert classification_compatible("rates_policy", "macro_event.macro_rates")
    assert not classification_compatible("rates_policy", "company_event.earnings_guidance")


def test_djnl_outcome_and_context_headlines_are_not_causal_proxy_inputs() -> None:
    assert has_s05p3_outcome_or_context("Dow Jones Futures Jump, Oil Prices Dive After Iran News")
    assert has_s05p3_outcome_or_context("Gold Rises After Israel-Lebanon Ceasefire -- Market Talk")
    assert has_s05p3_outcome_or_context("Fed Rate Decision And Earnings To Watch This Week")
