from __future__ import annotations

import sqlite3

import pytest

from recorder.s05_proxy_semantic_v5 import (
    V5PolicyConfig,
    V5Thresholds,
    ensure_s05p4_v5_schema,
    map_headline_v5,
    persist_v5_mapping,
    rank_proxy_candidates_v5,
    resolve_region_roles_v5,
)


_METRIC_KEYS = (
    "median_dollar_volume_score",
    "spread_quality",
    "intraday_trade_coverage",
    "stale_trade_quality",
    "market_session_overlap",
    "family_exposure",
    "beta_purity",
    "data_availability",
)


def _metrics(symbols: tuple[str, ...], value: float = 0.8) -> dict[str, dict[str, float]]:
    return {symbol: {key: value for key in _METRIC_KEYS} for symbol in symbols}


def _roles(mapping: object) -> set[tuple[str, str]]:
    return {(row.region_code, row.region_role) for row in mapping.regions}


def test_v5_thresholds_load_versioned_config_names() -> None:
    thresholds = V5Thresholds.from_config({
        "thresholds": {
            "region_confidence": 0.91,
            "proxy_top1_top2_margin": 0.07,
            "learned_direction_confidence": 0.86,
        }
    })
    assert thresholds.region == 0.91
    assert thresholds.proxy_top1_top2_margin == 0.07
    assert thresholds.learned_direction == 0.86


def test_v5_policy_config_loads_watch_only_and_production_targets() -> None:
    policy = V5PolicyConfig.from_config({
        "version": "s05p4-semantic-mapping-v5.4.1-research",
        "mode": "research_only_no_promotion",
        "thresholds": {"region_confidence": 0.9},
        "production_targets": {
            "tier_a_precision": 0.97,
            "critical_region_channel_errors": 0,
            "primary_proxy_precision": 0.95,
            "high_confidence_direction_precision": 0.9,
        },
        "small_sample_families_watch_only": ["china_taiwan"],
        "safety": {
            "direction_requires_learned_sign_agreement": True,
            "mixed_or_unclear_direction_is_observation_only": True,
            "post_event_ranking_features_forbidden": True,
            "canonical_promotion": False,
            "m254_m255_m256": False,
            "orders": False,
            "transmit": False,
            "live_routing": False,
            "threshold_promotion": False,
        },
    })
    assert policy.production_targets.tier_a_precision == 0.97
    assert policy.small_sample_families_watch_only == frozenset({"china_taiwan"})


def test_v5_boj_direction_is_split_and_requires_learned_confirmation() -> None:
    mapping = map_headline_v5("Bank of Japan unexpectedly raises interest rates by 25 basis points")
    assert ("JP", "actor") in _roles(mapping)
    assert mapping.proxy_family == "japan_rates_fx"
    assert mapping.event_direction == "tightening"
    assert {row.channel_code for row in mapping.channels} >= {"rates_discount_rate", "fx_currency"}
    assert mapping.primary_proxy_selected == ""
    assert mapping.proxy_candidates[0].symbol == "FXY"
    assert mapping.proxy_abstain_reason == "missing_pre_event_metrics"
    assert mapping.event_direction_rule_strength >= 0.90
    assert mapping.event_direction_confidence == 0.0
    assert mapping.directional_strict is False
    assert mapping.trade_candidate_eligible is False
    assert mapping.reaction_observation_eligible is True


def test_v5_1_central_bank_speaker_advocacy_is_not_a_decision() -> None:
    mapping = map_headline_v5("Fed's Waller advocates a December rate cut")
    assert mapping.event_type == "central_bank_communication"


def test_v5_1_central_bank_official_rate_action_is_a_decision() -> None:
    mapping = map_headline_v5("FOMC cuts target rate by 25 basis points")
    assert mapping.event_type == "central_bank_decision"


def test_v5_1_central_bank_minutes_override_speaker_language() -> None:
    mapping = map_headline_v5("Fed minutes show most officials favored a cut")
    assert mapping.event_type == "central_bank_minutes"


def test_v5_1_central_bank_conditional_outlook_is_not_a_decision() -> None:
    mapping = map_headline_v5("Fed policy rate could fall at the next meeting")
    assert mapping.event_type == "central_bank_outlook_commentary"


@pytest.mark.parametrize(
    "headline",
    (
        "Reported Earlier, Japan Interest Rate Decision Unchanged At 0.75%",
        "Reported Earlier, Japan Interest Rate Decision Unchnaged At 0.75%",
        "Reported Earlier, Japan Interest Rate Decision 1.00% Vs. 1.00% Est.; 0.75% Prior",
    ),
)
def test_v5_2_provider_calendar_rate_result_is_a_decision(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_decision"
    assert ("JP", "directly_affected") in _roles(mapping)
    assert ("JP", "market_expression") in _roles(mapping)


def test_v5_2_central_bank_calendar_preview_is_not_a_decision() -> None:
    mapping = map_headline_v5("Fed rate decision tomorrow")
    assert mapping.event_type == "central_bank_calendar_preview"


def test_v5_2_decision_clause_outranks_following_speaker_comment() -> None:
    mapping = map_headline_v5(
        "FOMC cuts rates 25 bps; Powell says future decisions will be data-dependent"
    )
    assert mapping.event_type == "central_bank_decision"
    assert "central_bank_communication" in mapping.referenced_event_types
    assert "central_bank_outlook_commentary" in mapping.referenced_event_types


def test_v5_directional_trade_needs_semantic_learned_and_external_gates() -> None:
    mapping = map_headline_v5(
        "FOMC raises interest rates by 25 basis points",
        pre_event_metrics=_metrics(("IEF", "UUP")),
        pre_event_feature_snapshot_id="pit-rates-20260721",
        learned_proxy_direction="negative",
        learned_direction_confidence=0.92,
        learned_direction_snapshot_id="reaction-rates-v1",
        event_direction_empirical_precision=0.91,
        event_direction_calibrated_confidence=0.90,
        direction_calibration_snapshot_id="direction-calibration-v1",
        market_data_quality_pass=True,
        liquidity_pass=True,
        policy_gate_pass=True,
        human_adjudicated_news_content_type="central_bank_policy_event",
        content_type_adjudication_id="set-a-content-adjudication-test",
    )
    assert mapping.primary_proxy_selected == "IEF"
    assert mapping.directional_strict is True
    assert mapping.trade_candidate_eligible is True
    assert mapping.directional_trade_candidate_eligible is True


def test_v5_export_controls_resolve_actor_target_and_semiconductor_channel() -> None:
    mapping = map_headline_v5(
        "US imposes export restrictions on China over advanced semiconductors"
    )
    roles = _roles(mapping)
    assert ("US", "actor") in roles
    assert ("CN", "target") in roles
    assert ("TW", "supply_chain") in roles
    assert mapping.region_scope == "cross_border"
    assert {row.channel_code for row in mapping.channels} >= {
        "trade_export_control",
        "semiconductor_supply_chain",
    }
    assert mapping.proxy_family == "semis_supply"
    assert [row.symbol for row in mapping.proxy_candidates[:2]] == ["SMH", "SOXX"]
    assert mapping.primary_proxy_selected == ""
    assert mapping.proxy_abstained is True
    assert "actor_target_inversion" not in mapping.critical_conflicts


@pytest.mark.parametrize(
    "headline",
    (
        "U.S. Imposes Sanctions On Vessels Linked To Iran",
        "US imposes sanctions on Iran",
        "United States imposes sanctions on Iran",
    ),
)
def test_v5_1_us_actor_token_variants(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert ("US", "actor") in _roles(mapping)
    actor = next(
        row for row in mapping.regions
        if row.region_code == "US" and row.region_role == "actor"
    )
    assert actor.relation_type == "subject_of_action"
    assert actor.action_status in {"executed", "officially_announced"}


def test_v5_2_us_central_command_is_direct_action_actor() -> None:
    mapping = map_headline_v5("U.S. Central Command launches strikes against Iran")
    assert ("US", "actor") in _roles(mapping)


@pytest.mark.parametrize(
    "headline",
    (
        "TSX rises with oil prices, U.S. stock markets mixed after Iran strike",
        "Reuters Cites U.S. Representative Telling UN Security Council, Strikes On Iran Continue",
        "Iran Rejected Temporary Ceasefire With U.S.; Halt To Strikes Was Discussed",
        "Countries Hosting U.S. Bases Would Be Lawful Targets If Trump Strikes Iran",
    ),
)
def test_v5_2_us_context_is_not_promoted_to_action_actor(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert ("US", "actor") not in _roles(mapping)


def test_v5_2_conditional_us_action_blocks_strict_and_observation() -> None:
    mapping = map_headline_v5(
        "Countries Hosting U.S. Bases Would Be Lawful Targets If Trump Strikes Iran",
        human_adjudicated_news_content_type="geopolitical_policy_statement",
        content_type_adjudication_id="set-a-content-adjudication-test",
    )
    assert "conditional_or_prospective_action" in mapping.critical_conflicts
    assert mapping.semantic_mapping_strict is False
    assert mapping.reaction_observation_eligible is False


def test_v5_2_cyber_deploy_does_not_create_military_channels() -> None:
    mapping = map_headline_v5("Hackers deploy their custom ApolloShadow malware")
    channels = {row.channel_code for row in mapping.channels}
    assert "cybersecurity_spending" in channels
    assert "defense_demand" not in channels
    assert "risk_off" not in channels


def test_v5_2_military_deploy_requires_military_object() -> None:
    mapping = map_headline_v5("US deploys additional troops to the region")
    channels = {row.channel_code for row in mapping.channels}
    assert {"risk_off", "defense_demand"} <= channels


def test_v5_2_unknown_unadjudicated_content_cannot_be_strict() -> None:
    mapping = map_headline_v5("Fed's Waller advocates a December rate cut")
    assert mapping.news_content_type == "unknown"
    assert mapping.content_type_status == "unknown"
    assert mapping.content_type_strict_eligible is False
    assert mapping.semantic_mapping_strict is False
    assert mapping.trade_candidate_eligible is False


def test_v5_2_human_adjudicated_content_can_unlock_semantic_gate() -> None:
    mapping = map_headline_v5(
        "Fed's Waller advocates a December rate cut",
        human_adjudicated_news_content_type="central_bank_communication",
        content_type_adjudication_id="set-a-content-adjudication-test",
    )
    assert mapping.content_type_status == "human_adjudicated"
    assert mapping.content_type_strict_eligible is True


def test_v5_3_inflation_release_is_not_central_bank_communication() -> None:
    mapping = map_headline_v5(
        "Fed's Favorite Inflation Gauge Rises More Than Expected"
    )
    assert mapping.news_content_type == "macro_data_release"
    assert mapping.event_type == "inflation_data_release"
    assert ("US", "actor") not in _roles(mapping)
    assert {("US", "directly_affected"), ("US", "market_expression")} <= _roles(mapping)
    primary = next(row for row in mapping.channels if row.channel_role == "primary")
    assert primary.channel_code == "inflation_pressure"
    assert primary.mechanism_direction == "increase"


def test_v5_3_price_recap_recovers_executed_authority_event() -> None:
    mapping = map_headline_v5(
        "Oil Prices Trim Losses As Department Of Treasury Issues Sanctions "
        "On Iran's Crude Shipments To China"
    )
    assert mapping.causal_eligible is False
    assert mapping.causal_exclusion_reason == "price_move_recap"
    assert mapping.recovered_event_candidate is True
    assert mapping.recovered_event_clause.startswith("Department Of Treasury Issues Sanctions")
    assert mapping.core_event_observation_eligible is True
    assert mapping.semantic_mapping_strict is False
    assert mapping.reaction_observation_eligible is False


def test_v5_3_connector_without_authority_action_is_not_recovered() -> None:
    mapping = map_headline_v5(
        "Oil Prices Trim Losses As Traders Debate Whether Supply Could Tighten"
    )
    assert mapping.recovered_event_candidate is False


def test_v5_3_official_opec_forecast_revision_is_energy_demand_event() -> None:
    mapping = map_headline_v5("OPEC cuts global oil-demand growth forecast")
    assert mapping.causal_eligible is True
    assert mapping.news_content_type == "official_forecast_revision"
    assert mapping.event_type == "official_energy_demand_forecast_revision"
    primary = next(row for row in mapping.channels if row.channel_role == "primary")
    assert primary.channel_code == "energy_demand"
    assert primary.mechanism_direction == "decrease"
    assert mapping.event_direction == "demand_outlook_down"
    assert mapping.proxy_family == "oil_demand"
    assert mapping.family_watch_only is True
    assert mapping.taxonomy_expansion_status == "pending_adjudication"
    assert mapping.taxonomy_strict_eligible is False
    assert mapping.semantic_mapping_strict is False
    assert mapping.strict_training_eligible is False
    assert mapping.sample_purpose == "taxonomy_expansion_research"
    assert mapping.reaction_observation_eligible is True
    assert mapping.trade_candidate_eligible is False


def test_v5_3_mixed_opec_forecast_revisions_have_mixed_direction() -> None:
    mapping = map_headline_v5(
        "OPEC Raises 2027 Global Oil-Demand Growth Forecast; "
        "OPEC Cuts 2026 Global Oil-Demand Growth Forecast"
    )
    assert mapping.event_type == "official_energy_demand_forecast_revision"
    assert mapping.event_direction == "mixed"
    assert mapping.directional_strict is False


@pytest.mark.parametrize(
    "headline",
    (
        "Kansas City Fed's Schmid Says Prepared Remarks Do Not Address March CPI",
        "Fed's Kugler Says Inflation Rate Has Gone Sideways",
        "Fed's Barkin Says CPI Data Was Encouraging",
        "Fed's Cook Says AI Could Have Unemployment Rise",
    ),
)
def test_v5_4_central_bank_speech_mentioning_macro_data_is_not_a_release(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_communication"
    assert mapping.news_content_type != "macro_data_release"
    assert mapping.content_type_status in {"unknown", "legacy_rule_unvalidated"}
    assert mapping.content_type_strict_eligible is False


def test_v5_4_direct_inflation_result_remains_a_release() -> None:
    mapping = map_headline_v5(
        "Fed's preferred inflation gauge rises 0.3%, above estimate"
    )
    assert mapping.event_type == "inflation_data_release"
    assert mapping.news_content_type == "macro_data_release"
    assert mapping.content_type_status == "v5_validated_rule"
    assert mapping.content_type_strict_eligible is True
    assert mapping.decision_subject_type == "not_applicable"
    assert mapping.decision_temporality == "not_applicable"


def test_v5_4_combined_gdp_and_inflation_result_is_macro_release() -> None:
    mapping = map_headline_v5(
        "US GDP rose 2.1% quarter over quarter; CPI rose 0.3% vs. 0.2% estimate"
    )
    assert mapping.event_type == "macro_data_release"
    assert "inflation_data_release" in mapping.referenced_event_types


@pytest.mark.parametrize(
    ("headline", "subject_type", "temporality"),
    (
        (
            "U.S. Treasury Secretary Bessent On Fed: Would Be Ready To Cut Rates",
            "external_commentator",
            "hypothetical",
        ),
        ("Fed's Collins Says Seems Prudent To Cut Rates Further", "official_speaker", "future_preference"),
        ("Fed's Miran Says Fed Needs To Cut Rates", "official_speaker", "future_preference"),
        ("Fed's Waller Says Fed Should Continue To Cut Rates", "official_speaker", "future_preference"),
    ),
)
def test_v5_4_speaker_or_external_rate_preference_is_not_a_decision(
    headline: str, subject_type: str, temporality: str
) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type in {"central_bank_communication", "central_bank_outlook_commentary"}
    assert mapping.event_type != "central_bank_decision"
    assert mapping.decision_subject_type == subject_type
    assert mapping.decision_temporality == temporality


@pytest.mark.parametrize(
    ("headline", "subject_type"),
    (
        ("FOMC cuts target rate by 25 bps", "committee"),
        ("Fed holds rates unchanged", "institution"),
        ("BOJ raises policy rate", "institution"),
        ("ECB cuts deposit rate", "institution"),
        ("Japan Interest Rate Decision: 0.75% vs. 0.75% est.", "institution"),
    ),
)
def test_v5_4_current_institutional_decision_requires_valid_subject(
    headline: str, subject_type: str
) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_decision"
    assert mapping.decision_subject_type == subject_type
    assert mapping.decision_temporality == "current_decision"


def test_v5_4_legacy_content_rule_cannot_unlock_semantic_strict() -> None:
    mapping = map_headline_v5(
        "Reuters Citing Iranian State TV: Iran Launches Ballistic Missiles At Israel"
    )
    assert mapping.content_type_status == "legacy_rule_unvalidated"
    assert mapping.content_type_strict_eligible is False
    assert mapping.semantic_mapping_strict is False


@pytest.mark.parametrize(
    "headline",
    (
        "Market Recap; President Says Can't Say Whether U.S. Will Strike Iran",
        "Market Recap; Oil Storage Crunch May Force Iran To Strike Deal",
        "Market Recap; Hopefully Russia Will Agree To Ceasefire",
        "Market Recap; Felt Strongly Iran Was Going To Attack First",
        "Market Recap; Attack Will Not Change Their Demands",
        "Market Recap; Says It Holds U.S. Joint Responsibility For Israel's Doha Attack",
        "Market Recap; Trump Says Iran Has Requested Emergency Talks After Military Strikes",
        "Market Recap; Fed's Rate Cut: Investor Fear Eases Further",
    ),
)
def test_v5_4_conditional_or_commentary_clause_is_not_recovered(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.recovered_event_candidate is False
    assert mapping.recovery_action_status == "not_applicable"
    assert mapping.core_event_observation_eligible is False


def test_v5_4_opec_forecast_preview_is_not_an_actual_revision() -> None:
    mapping = map_headline_v5("OPEC will publish its global oil-demand forecast tomorrow")
    assert mapping.event_type != "official_energy_demand_forecast_revision"
    assert mapping.taxonomy_expansion_requires_adjudication is False


def test_v5_4_1_human_approved_taxonomy_can_unlock_semantic_strict() -> None:
    mapping = map_headline_v5(
        "OPEC cuts global oil-demand growth forecast",
        taxonomy_adjudication_id="taxonomy-set-a-oil-demand-v1",
        taxonomy_expansion_status_override="human_approved",
    )
    assert mapping.taxonomy_expansion_status == "human_approved"
    assert mapping.taxonomy_strict_eligible is True
    assert mapping.semantic_mapping_strict is True
    assert mapping.strict_training_eligible is True
    assert mapping.sample_purpose == "standard_semantic_research"


def test_v5_4_1_rejected_taxonomy_stays_non_strict() -> None:
    mapping = map_headline_v5(
        "OPEC cuts global oil-demand growth forecast",
        taxonomy_adjudication_id="taxonomy-set-a-oil-demand-rejected-v1",
        taxonomy_expansion_status_override="rejected",
    )
    assert mapping.taxonomy_expansion_status == "rejected"
    assert mapping.taxonomy_strict_eligible is False
    assert mapping.semantic_mapping_strict is False
    assert mapping.strict_training_eligible is False
    assert mapping.reaction_observation_eligible is False
    assert mapping.sample_purpose == "taxonomy_rejected_context"


def test_v5_4_1_taxonomy_status_requires_valid_lineage() -> None:
    with pytest.raises(ValueError, match="must be provided together"):
        map_headline_v5(
            "OPEC cuts global oil-demand growth forecast",
            taxonomy_expansion_status_override="human_approved",
        )
    with pytest.raises(ValueError, match="must be provided together"):
        map_headline_v5(
            "OPEC cuts global oil-demand growth forecast",
            taxonomy_adjudication_id="taxonomy-id-without-explicit-status",
        )
    with pytest.raises(ValueError, match="only valid for an expansion candidate"):
        map_headline_v5(
            "FOMC cuts target rate by 25 bps",
            taxonomy_adjudication_id="invalid-taxonomy-lineage",
        )


@pytest.mark.parametrize(
    "headline",
    (
        "Fed's Kashkari Says 10-year Yield Rose Even As Fed Cut Rates",
        "Powell Says Fed Cut Rates At The Previous Meeting",
    ),
)
def test_v5_4_1_speaker_prior_decision_is_communication(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_communication"
    assert "central_bank_decision" in mapping.referenced_event_types
    assert mapping.decision_subject_type == "official_speaker"
    assert mapping.decision_temporality == "prior_decision_reference"


def test_v5_4_1_market_recap_prior_decision_is_referenced_only() -> None:
    mapping = map_headline_v5("Markets rallied after Fed cut rates")
    assert mapping.event_type in {"market_recap", "price_move_recap"}
    assert "central_bank_decision" in mapping.referenced_event_types
    assert mapping.decision_temporality == "prior_decision_reference"


@pytest.mark.parametrize(
    "headline",
    (
        "Fed's Waller Says Policy Is Restrictive Ahead Of The July Meeting",
        "Fed's Bowman Says Inflation Remains High Before This Week's Meeting",
    ),
)
def test_v5_4_1_speaker_outranks_calendar_preview(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_communication"
    assert "central_bank_calendar_preview" in mapping.referenced_event_types


@pytest.mark.parametrize(
    "headline",
    (
        "Powell Says CPI Was 3.0% Vs. 2.9% Estimate",
        "Barkin Says Unemployment Was 4.2% Vs. 4.1% Estimate",
    ),
)
def test_v5_4_1_speaker_numeric_macro_reference_is_not_release(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_communication"
    assert mapping.news_content_type != "macro_data_release"
    assert mapping.content_type_strict_eligible is False


def test_v5_4_1_release_authority_numeric_template_is_macro_release() -> None:
    mapping = map_headline_v5("BLS Says CPI Rose 0.3% Vs. 0.2% Estimate")
    assert mapping.event_type == "inflation_data_release"
    assert mapping.news_content_type == "macro_data_release"
    assert mapping.content_type_status == "v5_validated_rule"


@pytest.mark.parametrize(
    ("headline", "referenced_event"),
    (
        ("Powell Says ISM Was 50.1 Vs. 49.9 Estimate", "macro_data_release"),
        ("Powell Says BLS Reported CPI At 3.0% Vs. 2.9% Estimate", "inflation_data_release"),
    ),
)
def test_v5_4_1_authority_mention_inside_speaker_quote_is_not_release(
    headline: str,
    referenced_event: str,
) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == "central_bank_communication"
    assert referenced_event in mapping.referenced_event_types
    assert mapping.news_content_type != "macro_data_release"


@pytest.mark.parametrize(
    ("headline", "event_type"),
    (
        ("ISM Reports PMI At 50.1 Vs. 49.9 Estimate", "macro_data_release"),
        ("BLS Reports CPI At 0.3% Vs. 0.2% Estimate", "inflation_data_release"),
    ),
)
def test_v5_4_1_release_authority_must_be_grammatical_subject(
    headline: str,
    event_type: str,
) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.event_type == event_type
    assert mapping.news_content_type == "macro_data_release"


def test_v5_issuer_domicile_is_not_used_as_geopolitical_region() -> None:
    mapping = map_headline_v5("TSMC hits a 52-week high on strong AI demand")
    assert ("TW", "target") not in _roles(mapping)
    assert ("TW", "directly_affected") not in _roles(mapping)
    assert mapping.semantic_mapping_strict is False
    assert mapping.trade_candidate_eligible is False
    assert "issuer_domicile_used_as_event_region" not in mapping.critical_conflicts


def test_v5_energy_sanctions_use_oil_supply_and_commodity_primary() -> None:
    mapping = map_headline_v5("US imposes sanctions on Iranian crude oil exports")
    primary = next(row for row in mapping.channels if row.channel_role == "primary")
    assert primary.channel_code == "energy_supply"
    assert primary.mechanism_direction == "decrease"
    assert mapping.proxy_family == "oil_supply"
    assert mapping.primary_proxy_selected == ""
    assert mapping.proxy_abstain_reason == "missing_pre_event_metrics"
    assert [row.symbol for row in mapping.proxy_candidates[:3]] == ["USO", "XLE", "XOP"]


@pytest.mark.parametrize(
    "headline",
    (
        "Cramer discusses the geopolitical implications of Iran tensions",
        "Defense stocks look cheap after months of conflict",
        "Oil prices jump as traders digest yesterday's supply news",
        "Fed policy outlook: five ETFs to watch this week",
    ),
)
def test_v5_negative_fixtures_abstain_from_strict_mapping(headline: str) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.semantic_mapping_strict is False
    assert mapping.directional_strict is False
    assert mapping.trade_candidate_eligible is False


@pytest.mark.parametrize(
    "headline, expected_reason",
    (
        (
            "Dow Jones Futures Loom As U.S.-Israeli Strikes Continue; Iran Vows Revenge",
            "market_or_price_reaction_framing",
        ),
        (
            "Precious Metals Rise After Trump Agrees to Ceasefire With Iran",
            "market_or_price_reaction_framing",
        ),
        (
            "Dan Ives Says Iran-Israel Ceasefire Could Be Most Bullish Outcome Yet",
            "opinion_or_impact_analysis",
        ),
        (
            "OPEC+ Countries Start Online Meeting To Discuss Proposed 2027 Baselines",
            "preview_meeting_or_proposal",
        ),
        (
            "President Says He Was Aware Of Plans To Attack Iran",
            "non_material_or_secondary_commentary",
        ),
    ),
)
def test_v5_generalized_causal_refinement_rejects_non_event_framing(
    headline: str, expected_reason: str
) -> None:
    mapping = map_headline_v5(headline)
    assert mapping.causal_eligible is False
    assert mapping.causal_exclusion_reason == expected_reason
    assert mapping.semantic_mapping_strict is False


def test_v5_quoted_target_does_not_become_actor_target_inversion() -> None:
    mapping = map_headline_v5(
        "Iran's Foreign Minister Says Launching An Attack On Iranian Sites Would Be A Mistake"
    )
    assert "actor_target_inversion" not in mapping.critical_conflicts
    assert ("IR", "actor") not in _roles(mapping)


def test_v5_unclear_direction_remains_observation_only() -> None:
    mapping = map_headline_v5("Fed's Waller says policy will depend on incoming inflation data")
    assert mapping.event_direction in {"mixed", "unclear"}
    assert mapping.directional_strict is False
    assert mapping.directional_trade_candidate_eligible is False


def test_v5_proxy_ranker_rejects_post_event_features() -> None:
    mapping = map_headline_v5("OPEC+ agrees to cut oil output by 1 million barrels per day")
    with pytest.raises(ValueError, match="forbidden"):
        rank_proxy_candidates_v5(
            mapping.proxy_family,
            mapping.proxy_family_confidence,
            mapping.event_direction,
            mapping.regions,
            mapping.channels,
            pre_event_metrics={"USO": {"5m_return": 1.0}},
        )


def test_v5_proxy_ranker_abstains_when_top_scores_are_too_close() -> None:
    mapping = map_headline_v5("China begins military drills around Taiwan")
    metrics = _metrics(("EWT",), 0.5)
    metrics.update(_metrics(("FXI",), 0.65))
    _, selected, _, gap, abstained, reason = rank_proxy_candidates_v5(
        "china_taiwan",
        0.92,
        mapping.event_direction,
        mapping.regions,
        mapping.channels,
        pre_event_metrics=metrics,
        pre_event_feature_snapshot_id="pit-china-1",
    )
    assert gap < 0.06
    assert selected == ""
    assert abstained is True
    assert reason == "top1_top2_margin_too_small"


def test_v5_research_schema_and_persistence_are_idempotent() -> None:
    mapping = map_headline_v5("Iran launches missile barrage following Israeli strikes")
    with sqlite3.connect(":memory:") as conn:
        ensure_s05p4_v5_schema(conn)
        first_key = persist_v5_mapping(
            conn,
            event_cluster_id=17,
            snapshot_id="snapshot-a",
            mapping=mapping,
            build_id="test",
            config_hash="cfg",
        )
        second_key = persist_v5_mapping(
            conn,
            event_cluster_id=17,
            snapshot_id="snapshot-a",
            mapping=mapping,
            build_id="test",
            config_hash="cfg",
        )
        assert first_key == second_key
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_semantic_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_proxy_ranking_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_policy_evaluation_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_event_regions").fetchone()[0] == len(mapping.regions)
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_event_channels").fetchone()[0] == len(mapping.channels)
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_event_directions").fetchone()[0] == sum(
            row.direction_type != "proxy" for row in mapping.directions
        )
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_family_candidates").fetchone()[0] == len(mapping.family_candidates)
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_proxy_candidates").fetchone()[0] == len(mapping.proxy_candidates)
        immutable_runs_before = {
            table: conn.execute(
                f"SELECT created_at_utc,content_hash FROM {table}"
            ).fetchall()
            for table in (
                "s05p4_v5_semantic_runs",
                "s05p4_v5_proxy_ranking_runs",
                "s05p4_v5_policy_evaluation_runs",
            )
        }
        child_rows_before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
            for table in (
                "s05p4_v5_event_regions",
                "s05p4_v5_event_channels",
                "s05p4_v5_event_directions",
                "s05p4_v5_family_candidates",
                "s05p4_v5_proxy_candidates",
            )
        }
        persist_v5_mapping(
            conn,
            event_cluster_id=17,
            snapshot_id="snapshot-a",
            mapping=mapping,
            build_id="different-rerun-build",
            config_hash="cfg",
        )
        immutable_runs_after = {
            table: conn.execute(
                f"SELECT created_at_utc,content_hash FROM {table}"
            ).fetchall()
            for table in immutable_runs_before
        }
        assert immutable_runs_before == immutable_runs_after
        child_rows_after = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2,3").fetchall()
            for table in child_rows_before
        }
        assert child_rows_before == child_rows_after


def test_v5_2_content_adjudication_has_distinct_durable_lineage() -> None:
    headline = "Fed's Waller advocates a December rate cut"
    unreviewed = map_headline_v5(headline)
    adjudicated = map_headline_v5(
        headline,
        human_adjudicated_news_content_type="central_bank_communication",
        content_type_adjudication_id="set-a-content-adjudication-test",
    )
    with sqlite3.connect(":memory:") as conn:
        unreviewed_keys = persist_v5_mapping(
            conn,
            event_cluster_id=19,
            snapshot_id="event-snapshot",
            mapping=unreviewed,
        )
        adjudicated_keys = persist_v5_mapping(
            conn,
            event_cluster_id=19,
            snapshot_id="event-snapshot",
            mapping=adjudicated,
        )
        assert unreviewed_keys.semantic_run_key != adjudicated_keys.semantic_run_key
        rows = conn.execute(
            """SELECT content_type_status,content_type_strict_eligible,
                      content_type_adjudication_id
                 FROM s05p4_v5_semantic_runs
                ORDER BY content_type_status"""
        ).fetchall()
        assert rows == [
            ("human_adjudicated", 1, "set-a-content-adjudication-test"),
            ("unknown", 0, ""),
        ]


def test_v5_4_decision_recovery_and_taxonomy_status_are_durable() -> None:
    mappings = (
        map_headline_v5("FOMC cuts target rate by 25 bps"),
        map_headline_v5(
            "Oil Prices Trim Losses As Department Of Treasury Issues Sanctions "
            "On Iran's Crude Shipments To China"
        ),
        map_headline_v5("OPEC cuts global oil-demand growth forecast"),
    )
    with sqlite3.connect(":memory:") as conn:
        for cluster_id, mapping in enumerate(mappings, start=201):
            persist_v5_mapping(
                conn,
                event_cluster_id=cluster_id,
                snapshot_id="v5.4-status-snapshot",
                mapping=mapping,
            )
        rows = conn.execute(
            """SELECT decision_subject_type,decision_temporality,
                      recovery_action_status,taxonomy_expansion_requires_adjudication,
                      taxonomy_expansion_status,taxonomy_adjudication_id,
                      taxonomy_strict_eligible,sample_purpose,strict_training_eligible
                 FROM s05p4_v5_semantic_runs
                ORDER BY event_cluster_id"""
        ).fetchall()
        assert rows == [
            (
                "committee", "current_decision", "not_applicable", 0,
                "not_applicable", "", 1, "standard_semantic_research", 0,
            ),
            (
                "not_applicable", "not_applicable", "officially_announced", 0,
                "not_applicable", "", 1, "standard_semantic_research", 0,
            ),
            (
                "not_applicable", "not_applicable", "not_applicable", 1,
                "pending_adjudication", "", 0, "taxonomy_expansion_research", 0,
            ),
        ]


def test_v5_region_resolver_flags_no_actor_target_inversion_for_cross_border_policy() -> None:
    _, scope, confidence, conflicts = resolve_region_roles_v5(
        "US imposes export controls on China over advanced chips"
    )
    assert scope == "cross_border"
    assert confidence >= 0.90
    assert "actor_target_inversion" not in conflicts


def test_v5_content_and_event_type_survive_family_abstention() -> None:
    mapping = map_headline_v5("TSMC hits a 52-week high on strong AI demand")
    assert mapping.proxy_family == ""
    assert mapping.news_content_type != ""
    assert mapping.event_type == mapping.news_content_type


def test_v5_critical_conflict_blocks_proxy_reaction_observation() -> None:
    mapping = map_headline_v5("US strike against US targets and imposes sanctions")
    assert "actor_target_inversion" in mapping.critical_conflicts
    assert mapping.reaction_observation_eligible is False


def test_v5_watch_only_family_cannot_become_trade_candidate() -> None:
    mapping = map_headline_v5(
        "China begins military drills around Taiwan",
        pre_event_metrics={
            **_metrics(("EWT",), 0.95),
            **_metrics(("FXI",), 0.20),
        },
        pre_event_feature_snapshot_id="pit-china-strong-gap",
        market_data_quality_pass=True,
        liquidity_pass=True,
        policy_gate_pass=True,
        policy_config=V5PolicyConfig(),
    )
    assert mapping.primary_proxy_selected == "EWT"
    assert mapping.family_watch_only is True
    assert mapping.trade_candidate_eligible is False


def test_v5_all_allowed_pit_metrics_affect_proxy_score() -> None:
    mapping = map_headline_v5("OPEC+ agrees to cut oil output by 1 million barrels per day")
    baseline = _metrics(("USO", "XLE", "XOP"), 0.5)
    improved = _metrics(("USO", "XLE", "XOP"), 0.5)
    for key in ("stale_trade_quality", "market_session_overlap", "family_exposure"):
        improved["USO"][key] = 1.0
    baseline_rows, *_ = rank_proxy_candidates_v5(
        mapping.proxy_family,
        mapping.proxy_family_confidence,
        mapping.event_direction,
        mapping.regions,
        mapping.channels,
        pre_event_metrics=baseline,
        pre_event_feature_snapshot_id="pit-oil-baseline",
    )
    improved_rows, *_ = rank_proxy_candidates_v5(
        mapping.proxy_family,
        mapping.proxy_family_confidence,
        mapping.event_direction,
        mapping.regions,
        mapping.channels,
        pre_event_metrics=improved,
        pre_event_feature_snapshot_id="pit-oil-improved",
    )
    base_uso = next(row for row in baseline_rows if row.symbol == "USO")
    improved_uso = next(row for row in improved_rows if row.symbol == "USO")
    assert improved_uso.final_score > base_uso.final_score


def test_v5_incomplete_pit_metrics_cannot_select_primary_proxy() -> None:
    mapping = map_headline_v5(
        "FOMC raises interest rates by 25 basis points",
        pre_event_metrics={
            "IEF": {"median_dollar_volume_score": 0.9},
            "UUP": {"median_dollar_volume_score": 0.9},
        },
        pre_event_feature_snapshot_id="pit-incomplete",
    )
    assert mapping.primary_proxy_selected == ""
    assert mapping.proxy_abstain_reason == "incomplete_pre_event_metrics"


def test_v5_feature_snapshot_changes_ranking_and_policy_keys_only() -> None:
    metrics = _metrics(("IEF", "UUP"), 0.8)
    first = map_headline_v5(
        "FOMC raises interest rates by 25 basis points",
        pre_event_metrics=metrics,
        pre_event_feature_snapshot_id="pit-rates-a",
    )
    second = map_headline_v5(
        "FOMC raises interest rates by 25 basis points",
        pre_event_metrics=metrics,
        pre_event_feature_snapshot_id="pit-rates-b",
    )
    with sqlite3.connect(":memory:") as conn:
        first_keys = persist_v5_mapping(
            conn, event_cluster_id=23, snapshot_id="event-snapshot", mapping=first
        )
        second_keys = persist_v5_mapping(
            conn, event_cluster_id=23, snapshot_id="event-snapshot", mapping=second
        )
        assert first_keys.semantic_run_key == second_keys.semantic_run_key
        assert first_keys.proxy_ranking_run_key != second_keys.proxy_ranking_run_key
        assert first_keys.policy_evaluation_run_key != second_keys.policy_evaluation_run_key
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_semantic_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_proxy_ranking_runs").fetchone()[0] == 2


def test_v5_external_gate_state_changes_policy_key_only() -> None:
    metrics = _metrics(("IEF", "UUP"), 0.8)
    base_kwargs = {
        "pre_event_metrics": metrics,
        "pre_event_feature_snapshot_id": "pit-rates-fixed",
    }
    closed = map_headline_v5(
        "FOMC raises interest rates by 25 basis points", **base_kwargs
    )
    open_gates = map_headline_v5(
        "FOMC raises interest rates by 25 basis points",
        **base_kwargs,
        market_data_quality_pass=True,
        liquidity_pass=True,
        policy_gate_pass=True,
    )
    with sqlite3.connect(":memory:") as conn:
        closed_keys = persist_v5_mapping(
            conn, event_cluster_id=29, snapshot_id="event-snapshot", mapping=closed
        )
        open_keys = persist_v5_mapping(
            conn, event_cluster_id=29, snapshot_id="event-snapshot", mapping=open_gates
        )
        assert closed_keys.semantic_run_key == open_keys.semantic_run_key
        assert closed_keys.proxy_ranking_run_key == open_keys.proxy_ranking_run_key
        assert closed_keys.policy_evaluation_run_key != open_keys.policy_evaluation_run_key


def test_v5_attack_and_ceasefire_mix_is_not_directional() -> None:
    mapping = map_headline_v5("Israel attacks Iran as a ceasefire takes effect")
    assert mapping.event_direction == "mixed"
    assert mapping.directional_strict is False


def test_v5_mixed_recap_and_new_event_clause_is_recovered_without_strict_promotion() -> None:
    mapping = map_headline_v5(
        "Dow Jones Futures Loom After Overnight Losses; Iran launches missile attack against Israel"
    )
    assert mapping.causal_eligible is False
    assert mapping.recovered_event_candidate is True
    assert "Iran launches missile attack" in mapping.recovered_event_clause
    assert mapping.core_event_observation_eligible is True
    assert mapping.reaction_observation_eligible is False
    assert mapping.mapping_tier == "D_recovered_event_candidate"


def test_v5_resolver_supports_multiple_actors_and_targets() -> None:
    regions, scope, _, conflicts = resolve_region_roles_v5(
        "US strike against Iran; Israel strike against Syria"
    )
    roles = {(row.region_code, row.region_role) for row in regions}
    assert {("US", "actor"), ("IL", "actor")} <= roles
    assert {("IR", "target"), ("ME", "target")} <= roles
    assert scope == "cross_border"
    assert "actor_target_inversion" not in conflicts


def test_v5_transaction_rolls_back_children_and_parent_on_failure() -> None:
    mapping = map_headline_v5("Iran launches missile barrage following Israeli strikes")
    with sqlite3.connect(":memory:") as conn:
        ensure_s05p4_v5_schema(conn)
        conn.execute(
            """CREATE TRIGGER fail_v5_proxy_insert
               BEFORE INSERT ON s05p4_v5_proxy_candidates
               BEGIN SELECT RAISE(ABORT, 'forced rollback'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="forced rollback"):
            persist_v5_mapping(
                conn,
                event_cluster_id=41,
                snapshot_id="rollback-snapshot",
                mapping=mapping,
            )
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_semantic_runs").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM s05p4_v5_proxy_ranking_runs").fetchone()[0] == 0


def test_v5_schema_enforces_foreign_keys() -> None:
    with sqlite3.connect(":memory:") as conn:
        ensure_s05p4_v5_schema(conn)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """INSERT INTO s05p4_v5_event_regions(
                       semantic_run_key,region_code,region_role,confidence,
                       evidence_text,evidence_start,evidence_end,relation_type,
                       action_status,resolver_version
                   ) VALUES(
                       'missing-parent','US','actor',0.9,'US',0,2,
                       'subject_of_action','executed','test'
                   )"""
            )
