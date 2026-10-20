from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence

from .s05_news_semantic_closeout import classify_headline, normalize_headline
from .s05_proxy_semantic_closeout import infer_direction
from .s05_proxy_semantic_v4 import (
    classify_v4_causal_proxy_headline,
    match_proxy_rule_v4,
)


S05P4_V5_VERSION = "s05p4-semantic-mapping-v5.4.1-research"
V5_REGION_RESOLVER_VERSION = "s05p4-region-role-resolver-v5.4.1"
V5_CHANNEL_CLASSIFIER_VERSION = "s05p4-causal-channel-graph-v5.4.1"
V5_FAMILY_VALIDATOR_VERSION = "s05p4-family-validator-v5.4.1"
V5_PROXY_RANKER_VERSION = "s05p4-pre-event-proxy-ranker-v5.4.1"


@dataclass(frozen=True)
class V5Thresholds:
    region: float = 0.90
    channel: float = 0.90
    proxy_family: float = 0.90
    event_direction: float = 0.85
    primary_proxy: float = 0.90
    family_minimum: float = 0.80
    proxy_score_minimum: float = 0.65
    proxy_top1_top2_margin: float = 0.06
    learned_direction: float = 0.85

    @classmethod
    def from_config(cls, payload: Mapping[str, Any]) -> "V5Thresholds":
        values = payload.get("thresholds", payload)
        if not isinstance(values, Mapping):
            raise ValueError("v5 thresholds config must be a mapping")
        aliases = {
            "region_confidence": "region",
            "channel_confidence": "channel",
            "proxy_family_confidence": "proxy_family",
            "event_direction_confidence": "event_direction",
            "primary_proxy_confidence": "primary_proxy",
            "learned_direction_confidence": "learned_direction",
        }
        kwargs: dict[str, float] = {}
        valid = set(cls.__dataclass_fields__)
        for raw_key, raw_value in values.items():
            key = aliases.get(str(raw_key), str(raw_key))
            if key not in valid:
                raise ValueError(f"unsupported v5 threshold: {raw_key}")
            value = float(raw_value)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"v5 threshold must be in [0,1]: {raw_key}")
            kwargs[key] = value
        return cls(**kwargs)


@dataclass(frozen=True)
class V5ProductionTargets:
    tier_a_precision: float = 0.97
    critical_region_channel_errors: int = 0
    primary_proxy_precision: float = 0.95
    high_confidence_direction_precision: float = 0.90


@dataclass(frozen=True)
class V5SafetyPolicy:
    direction_requires_learned_sign_agreement: bool = True
    mixed_or_unclear_direction_is_observation_only: bool = True
    post_event_ranking_features_forbidden: bool = True
    canonical_promotion: bool = False
    m254_m255_m256: bool = False
    orders: bool = False
    transmit: bool = False
    live_routing: bool = False
    threshold_promotion: bool = False


@dataclass(frozen=True)
class V5PolicyConfig:
    version: str = S05P4_V5_VERSION
    mode: str = "research_only_no_promotion"
    thresholds: V5Thresholds = field(default_factory=V5Thresholds)
    production_targets: V5ProductionTargets = field(default_factory=V5ProductionTargets)
    small_sample_families_watch_only: frozenset[str] = field(
        default_factory=lambda: frozenset({
            "china_taiwan", "semis_supply", "cybersecurity", "grain_supply", "korea_risk",
            "oil_demand",
        })
    )
    safety: V5SafetyPolicy = field(default_factory=V5SafetyPolicy)

    @classmethod
    def from_config(cls, payload: Mapping[str, Any]) -> "V5PolicyConfig":
        version = str(payload.get("version", ""))
        if version != S05P4_V5_VERSION:
            raise ValueError("v5 policy config version does not match the semantic engine")
        targets_raw = payload.get("production_targets", {})
        safety_raw = payload.get("safety", {})
        if not isinstance(targets_raw, Mapping) or not isinstance(safety_raw, Mapping):
            raise ValueError("v5 production_targets and safety must be mappings")
        target_fields = set(V5ProductionTargets.__dataclass_fields__)
        safety_fields = set(V5SafetyPolicy.__dataclass_fields__)
        unknown_targets = set(targets_raw) - target_fields
        unknown_safety = set(safety_raw) - safety_fields
        if unknown_targets:
            raise ValueError(f"unsupported v5 production target: {sorted(unknown_targets)}")
        if unknown_safety:
            raise ValueError(f"unsupported v5 safety policy: {sorted(unknown_safety)}")
        targets = V5ProductionTargets(**{
            key: int(value) if key == "critical_region_channel_errors" else float(value)
            for key, value in targets_raw.items()
        })
        safety = V5SafetyPolicy(**{key: bool(value) for key, value in safety_raw.items()})
        if any((safety.canonical_promotion, safety.m254_m255_m256, safety.orders,
                safety.transmit, safety.live_routing, safety.threshold_promotion)):
            raise ValueError("research-only v5 config cannot enable promotion, M254+, or broker paths")
        return cls(
            version=version,
            mode=str(payload.get("mode", "research_only_no_promotion")),
            thresholds=V5Thresholds.from_config(payload),
            production_targets=targets,
            small_sample_families_watch_only=frozenset(
                str(value) for value in payload.get("small_sample_families_watch_only", ())
            ),
            safety=safety,
        )


@dataclass(frozen=True)
class RegionEvidence:
    region_code: str
    region_role: str
    confidence: float
    evidence_text: str
    evidence_start: int
    evidence_end: int
    relation_type: str = "contextual"
    action_status: str = "unclear"
    resolver_version: str = V5_REGION_RESOLVER_VERSION


@dataclass(frozen=True)
class ChannelEvidence:
    channel_code: str
    channel_role: str
    mechanism_direction: str
    confidence: float
    evidence_text: str
    rule_id: str
    classifier_version: str = V5_CHANNEL_CLASSIFIER_VERSION


@dataclass(frozen=True)
class DirectionEvidence:
    direction_type: str
    subject: str
    direction: str
    confidence: float
    evidence_text: str
    rule_strength: float
    empirical_precision: float | None
    calibrated_confidence: float | None


@dataclass(frozen=True)
class FamilyCandidate:
    proxy_family: str
    confidence: float
    required_evidence_pass: bool
    evidence_text: str
    validator_rule_id: str
    validator_version: str = V5_FAMILY_VALIDATOR_VERSION


@dataclass(frozen=True)
class ProxyCandidate:
    proxy_family: str
    symbol: str
    proxy_role: str
    expression_type: str
    semantic_relevance: float
    region_specificity: float
    channel_specificity: float
    tradability_score: float
    spread_quality: float
    intraday_trade_coverage: float
    stale_trade_quality: float
    market_session_overlap: float
    family_exposure: float
    proxy_purity: float
    data_coverage_score: float
    final_score: float
    rank: int
    expected_direction: str
    expected_direction_confidence: float
    ranker_version: str = V5_PROXY_RANKER_VERSION


@dataclass(frozen=True)
class V5SemanticMapping:
    normalized_headline: str
    provider: str
    causal_eligible: bool
    causal_exclusion_reason: str
    recovered_event_candidate: bool
    recovered_event_clause: str
    framing_clause: str
    core_event_observation_eligible: bool
    news_content_type: str
    content_type_status: str
    content_type_strict_eligible: bool
    content_type_adjudication_id: str
    event_type: str
    referenced_event_types: tuple[str, ...]
    region_scope: str
    region_confidence: float
    regions: tuple[RegionEvidence, ...]
    channel_confidence: float
    channels: tuple[ChannelEvidence, ...]
    event_direction: str
    event_direction_confidence: float
    event_direction_rule_strength: float
    event_direction_empirical_precision: float | None
    direction_calibration_snapshot_id: str
    directions: tuple[DirectionEvidence, ...]
    proxy_family: str
    proxy_family_confidence: float
    family_candidates: tuple[FamilyCandidate, ...]
    proxy_family_abstained: bool
    proxy_family_abstain_reason: str
    family_watch_only: bool
    primary_proxy_selected: str
    primary_proxy_confidence: float
    top1_top2_score_gap: float
    proxy_candidates: tuple[ProxyCandidate, ...]
    proxy_abstained: bool
    proxy_abstain_reason: str
    critical_conflicts: tuple[str, ...]
    semantic_mapping_strict: bool
    directional_strict: bool
    reaction_observation_eligible: bool
    trade_candidate_eligible: bool
    directional_trade_candidate_eligible: bool
    mapping_tier: str
    semantic_input_hash: str
    pre_event_feature_snapshot_id: str
    pre_event_feature_snapshot_hash: str
    learned_direction_snapshot_id: str
    learned_direction_snapshot_hash: str
    external_gate_state_hash: str
    threshold_config_hash: str
    policy_config_hash: str
    semantic_version: str = S05P4_V5_VERSION
    recovery_action_status: str = "not_applicable"
    decision_subject_type: str = "not_applicable"
    decision_temporality: str = "not_applicable"
    taxonomy_expansion_requires_adjudication: bool = False
    taxonomy_expansion_status: str = "not_applicable"
    taxonomy_adjudication_id: str = ""
    taxonomy_strict_eligible: bool = True
    sample_purpose: str = "standard_semantic_research"
    strict_training_eligible: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class MacroReleaseDetection:
    is_release: bool
    event_type: str = ""
    evidence_text: str = ""
    evidence_start: int = -1
    evidence_end: int = -1
    referenced_event_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class CentralBankDecisionDetection:
    is_current_decision: bool
    subject_type: str = "not_applicable"
    temporality: str = "not_applicable"
    evidence_text: str = ""


@dataclass(frozen=True)
class _ProxySpec:
    symbol: str
    expression_type: str
    semantic_relevance: float
    channel_specificity: float
    region_specificity: float


_US_TOKEN = r"(?<!\w)(?:u\.s\.|us|united states)(?!\w)"
_US_AUTHORITY_TOKEN = (
    r"(?<!\w)(?:u\.s\. central command|u\.s\.|us|united states|centcom|white house|"
    r"treasury|federal reserve|fed|fomc)(?!\w)"
)


_REGION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("US", re.compile(_US_AUTHORITY_TOKEN, re.I)),
    ("CN", re.compile(r"\b(?:china|chinese|beijing|pla)\b", re.I)),
    ("TW", re.compile(r"\b(?:taiwan|taiwanese|taipei)\b", re.I)),
    ("JP", re.compile(r"\b(?:japan|japanese|boj|bank of japan)\b", re.I)),
    ("EU", re.compile(r"\b(?:eurozone|european union|ecb|european central bank)\b", re.I)),
    ("GB", re.compile(r"\b(?:united kingdom|britain|british|bank of england|boe)\b", re.I)),
    ("IN", re.compile(r"\b(?:india|indian|reserve bank of india|rbi)\b", re.I)),
    ("IR", re.compile(r"\biran(?:ian)?\b", re.I)),
    ("IL", re.compile(r"\bisrael(?:i)?\b", re.I)),
    ("UA", re.compile(r"\bukrain(?:e|ian)\b", re.I)),
    ("RU", re.compile(r"\brussia(?:n)?\b", re.I)),
    ("KR", re.compile(r"\b(?:south korea|korean peninsula|seoul)\b", re.I)),
    ("KP", re.compile(r"\b(?:north korea|pyongyang|kim jong)\b", re.I)),
    ("ME", re.compile(r"\b(?:gaza|hamas|lebanon|hezbollah|houthi|syria)\b", re.I)),
)

_CENTRAL_BANK_REGIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("JP", re.compile(r"\b(?:boj|bank of japan)\b", re.I)),
    ("EU", re.compile(r"\b(?:ecb|european central bank)\b", re.I)),
    ("GB", re.compile(r"\b(?:boe|bank of england)\b", re.I)),
    ("IN", re.compile(r"\b(?:rbi|reserve bank of india)\b", re.I)),
    ("US", re.compile(r"\b(?:fed|fomc|federal reserve)\b", re.I)),
)

_ACTION_AUXILIARY = r"(?:(?:has|have|had|is|are|was|were)\s+)?"
_ACTION_ASPECT = r"(?:(?:begins?|began|starts?|started)\s+)?"
_PROSPECTIVE_ACTOR_PREFIX = re.compile(
    r"\b(?:if|unless|should|could|would|may|might|planning|preparing)\s*$",
    re.I,
)
_US_PROSPECTIVE_ACTOR = re.compile(
    rf"\bif\s+{_US_TOKEN}\s+(?:strikes?|attacks?|imposes?|bans?)\b|"
    r"\bif\s+(?:president\s+)?trump\s+(?:strikes?|attacks?|imposes?|bans?)\b|"
    rf"{_US_TOKEN}\s+(?:could|would|may|might|plans? to|prepares? to)\s+"
    r"(?:strike|attack|impose|ban)\b",
    re.I,
)
_ACTION_ACTOR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "US",
        re.compile(
            rf"(?:{_US_TOKEN}(?:\s+(?:military|armed forces?|forces?|central command|government|administration))?|"
            r"(?<!\w)(?:u\.s\. central command|centcom(?: forces?)?|white house|treasury|president trump|trump administration)(?!\w))"
            rf"\s+{_ACTION_AUXILIARY}{_ACTION_ASPECT}"
            r"(?:imposes?|issues?|restricts?|bans?|strikes?|sanctions?|approves?|orders?|announces?|launches?|attacks?)\b",
            re.I,
        ),
    ),
    ("CN", re.compile(r"\b(?:china|beijing|pla)(?:\s+(?:military|forces?|government))?\s+(?:(?:has|is|was)\s+)?(?:begins?\s+)?(?:imposes?|restricts?|bans?|drills?|launches?|sanctions?|announces?)\b", re.I)),
    ("IR", re.compile(r"\biran(?:ian)?(?:\s+(?:military|forces?|government))?\s+(?:(?:has|is|was)\s+)?(?:begins?\s+)?(?:launches?|fires?|attacks?|seizes?|closes?|reopens?|agrees?|rejects?)\b", re.I)),
    ("IL", re.compile(r"\bisrael(?:i)?(?:\s+(?:military|forces?|government))?\s+(?:(?:has|is|was)\s+)?(?:begins?\s+)?(?:launches?|strikes?|attacks?|orders?|agrees?|rejects?)\b", re.I)),
    ("RU", re.compile(r"\brussia(?:n)?(?:\s+(?:military|forces?|government))?\s+(?:(?:has|is|was)\s+)?(?:begins?\s+)?(?:launches?|strikes?|attacks?|invades?|cuts?|sanctions?|agrees?)\b", re.I)),
    ("UA", re.compile(r"\bukrain(?:e|ian)?(?:\s+(?:military|forces?|government))?\s+(?:(?:has|is|was)\s+)?(?:begins?\s+)?(?:launches?|strikes?|attacks?|agrees?|rejects?)\b", re.I)),
)

_TARGET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (
        code,
        re.compile(
            rf"\b(?:sanctions?|restrictions?|ban|embargo|strike|attack|missile|drills?)\b.{{0,80}}(?:on|against|around|targeting)\s+{pattern.pattern}",
            re.I,
        ),
    )
    for code, pattern in _REGION_PATTERNS
)

_MILITARY_ACTION = re.compile(
    r"\b(?:launches?|fires?|strikes?|attacks?|invades?|intercepts?|mobilizes?|drills?|military exercises?|military operation|airstrike|missile barrage|shooting starts)\b|"
    r"\bdeploy(?:s|ed|ing)?\b.{0,40}\b(?:troops?|forces?|carrier(?: strike group)?s?|aircraft|missiles?|battalions?|brigades?|warships?|soldiers?)\b",
    re.I,
)
_CEASEFIRE_ACTION = re.compile(r"\b(?:agrees? to|signs?|announces?|extends?|begins?)\b.{0,80}\b(?:ceasefire|truce)\b|\bceasefire takes effect\b", re.I)
_OIL_SUPPLY_ACTION = re.compile(
    r"\b(?:oil|crude|lng|opec\+?|hormuz|refinery|pipeline|tanker|vessels?|strategic petroleum reserve)\b.{0,140}\b(?:cuts?|raises?|halts?|stops?|starts?|resumes?|reopens?|closes?|shutdown|outage|seizes?|sanctions?|bans?|releases?|quota|compensation|capacity|transits?|flows?|passage)\b|"
    r"\b(?:cuts?|raises?|halts?|stops?|starts?|resumes?|reopens?|closes?|seizes?|bans?|releases?)\b.{0,140}\b(?:oil|crude|lng|opec\+?|hormuz|refinery|pipeline|tanker|vessels?)\b",
    re.I,
)
_RATE_POLICY_ACTION = re.compile(
    r"\b(?:fed|fomc|federal reserve|boj|bank of japan|ecb|european central bank|boe|bank of england|rbi|reserve bank of india)\b.{0,160}\b(?:rate|policy|inflation|basis points?|bps|tightening|easing|dovish|hawkish|restrictive|neutral)\b|\binterest rate decision\b",
    re.I,
)
_INFLATION_DATA_RELEASE = re.compile(
    r"\b(?:cpi|consumer price index|pce|personal consumption expenditures?|inflation gauge|"
    r"inflation rate|core inflation)\b.{0,140}"
    r"\b(?:rises?|rose|falls?|fell|increases?|increased|decreases?|decreased|"
    r"above|below|more than expected|less than expected|actual|est(?:imate)?\.?|prior|"
    r"month|monthly|quarter|quarterly|year[- ]over[- ]year)\b",
    re.I,
)
_MACRO_DATA_RELEASE = re.compile(
    r"\b(?:cpi|consumer price index|pce|personal consumption expenditures?|inflation gauge|"
    r"inflation rate|core inflation|nonfarm payrolls?|payrolls?|unemployment(?: rate)?|"
    r"retail sales|gross domestic product|gdp|pmi|purchasing managers? index|ism)\b"
    r".{0,160}\b(?:rises?|rose|falls?|fell|increases?|increased|decreases?|decreased|"
    r"above|below|beats?|misses?|more than expected|less than expected|actual|"
    r"est(?:imate)?\.?|prior|month|monthly|quarter|quarterly|year[- ]over[- ]year)\b",
    re.I,
)
_MACRO_INDICATOR = re.compile(
    r"\b(?:cpi|consumer price index|pce|personal consumption expenditures?|inflation gauge|"
    r"inflation rate|core inflation|nonfarm payrolls?|payrolls?|unemployment(?: rate)?|"
    r"retail sales|gross domestic product|gdp|pmi|purchasing managers? index|ism)\b",
    re.I,
)
_INFLATION_INDICATOR = re.compile(
    r"\b(?:cpi|consumer price index|pce|personal consumption expenditures?|inflation gauge|"
    r"inflation rate|core inflation)\b",
    re.I,
)
_MACRO_RELEASE_SPEAKER_CONTEXT = re.compile(
    r"\b(?:fed(?:eral reserve)?(?: bank)?(?: of [a-z ]+)?['â€™]s\s+[a-z][a-z.'-]*|"
    r"fed\s+(?:governor|president|chair|official)\s+[a-z][a-z.'-]*|"
    r"[a-z][a-z.'-]*\s+(?:says?|said|warns?|warned|argues?|argued|believes?|believed|"
    r"expects?|expected))\b|\b(?:prepared remarks?|speech|interview)\b",
    re.I,
)
_MACRO_RELEASE_AUTHORITY = re.compile(
    r"\b(?:bureau of labor statistics|bls|bureau of economic analysis|bea|census bureau|"
    r"labor department|department of labor|commerce department|department of commerce|"
    r"institute for supply management|ism|s&p global)\b",
    re.I,
)
_MACRO_RELEASE_AUTHORITY_SUBJECT = re.compile(
    r"^(?:[!\s'\"])*(?:(?:reuters|bloomberg)\s+(?:reports?|says?)\s+)?"
    r"(?:bureau of labor statistics|bls|bureau of economic analysis|bea|census bureau|"
    r"labor department|department of labor|commerce department|department of commerce|"
    r"institute for supply management|ism|s&p global)\b.{0,40}"
    r"(?:\b(?:reports?|says?|announces?|releases?|publishes?|shows?)\b|[:,-])",
    re.I,
)
_MACRO_DIRECT_SUBJECT_RESULT = re.compile(
    r"^(?:[!\s'\"])*(?:(?:u\.s\.|us|united states)\s+)?"
    r"(?:cpi|consumer price index|pce|personal consumption expenditures?|inflation gauge|"
    r"inflation rate|core inflation|nonfarm payrolls?|payrolls?|unemployment(?: rate)?|"
    r"retail sales|gross domestic product|gdp|pmi|purchasing managers? index|ism)\b"
    r".{0,45}\b(?:rises?|rose|falls?|fell|increases?|increased|decreases?|decreased|"
    r"grew|growth|contracted|added|lost|accelerated|slowed|unchanged|at)\b",
    re.I,
)
_MACRO_PROVIDER_RESULT_TEMPLATE = re.compile(
    r"\b(?:actual|estimate|est\.?|prior|previous)\b.{0,100}\b(?:actual|estimate|est\.?|prior|previous)\b|"
    r"\b[-+]?\d+(?:\.\d+)?%?\s+(?:vs\.?|versus)\s+[-+]?\d+(?:\.\d+)?%?.{0,40}\b(?:est\.?|estimate|prior|previous)\b|"
    r"\b(?:month|monthly|quarter|quarterly|year[- ]over[- ]year|yoy|mom|qoq)\b.{0,80}"
    r"\b[-+]?\d+(?:\.\d+)?%\b",
    re.I,
)
_FED_PREFERRED_GAUGE_RESULT = re.compile(
    r"\bfed['â€™]s\s+(?:favorite|preferred)\s+(?:inflation\s+)?gauge\b.{0,80}"
    r"\b(?:rises?|rose|falls?|fell|increases?|increased|decreases?|decreased|"
    r"above|below|more than expected|less than expected|at\s+[-+]?\d)\b",
    re.I,
)
_US_MACRO_DATA_CUE = re.compile(
    rf"\bFed['’]s\s+(?:favorite|preferred)\b|{_US_TOKEN}.{0,50}"
    r"\b(?:cpi|pce|payrolls?|unemployment|retail sales|gdp|pmi|ism)\b|"
    r"\b(?:bureau of labor statistics|bls|bureau of economic analysis|bea|census bureau|"
    r"labor department|commerce department|ism|s&p global)\b",
    re.I,
)
_OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION = re.compile(
    r"\b(?:opec\+?|international energy agency|iea|energy information administration|eia)\b"
    r".{0,180}\b(?:raises?|raised|cuts?|cut|lowers?|lowered|reduces?|reduced|revises?|revised)\b"
    r".{0,180}\b(?:oil[- ]demand|crude[- ]demand|energy[- ]demand|demand growth)\b|"
    r"\b(?:opec\+?|international energy agency|iea|energy information administration|eia)\b"
    r".{0,220}\b(?:oil[- ]demand|crude[- ]demand|energy[- ]demand|demand growth)\b"
    r".{0,100}\b(?:forecast|outlook|projection)\b.{0,80}"
    r"\b(?:raised|increased|cut|lowered|reduced|revised)\b",
    re.I,
)
_EMBEDDED_AUTHORITY_ACTION = re.compile(
    r"^(?:department of (?:the )?treasury|u\.s\. treasury|treasury|white house|"
    r"federal reserve|fed|fomc|boj|bank of japan|ecb|european central bank|"
    r"opec\+?|iea|eia|centcom|u\.s\. central command)\b.{0,80}"
    r"\b(?:issues?|imposes?|announces?|orders?|approves?|launches?|cuts?|raises?|"
    r"halts?|resumes?|lifts?|removes?|restricts?|bans?)\b.{0,180}",
    re.I,
)
_RECOVERY_NON_MATERIAL = re.compile(
    r"\b(?:can't|cannot|can not)\s+say\s+whether\b|\b(?:may|might|could|would|should)\b|"
    r"\b(?:hopefully|hope(?:s|d)?|felt|believed|thinks?|expects?)\b|"
    r"\b(?:if|unless)\b|\bwill\s+not\s+change\b|\bnot\s+carried\s+out\b|"
    r"\b(?:demands?|requests?|requested|urges?|calls?\s+on)\b.{0,80}\b(?:strike|attack|deal|ceasefire|cut|talks?)\b|"
    r"^\s*(?:[a-z][a-z.'-]*\s+){0,4}(?:says?|said|claims?|claimed|reports?|reported)\b|"
    r"\bholds?\b.{0,60}\bresponsibility\b|"
    r"\b(?:fed|fomc|boj|ecb|boe|rbi)['â€™]s\s+(?:rate|policy)\s+(?:cut|hike|decision)\b",
    re.I,
)
_RECOVERY_CONFIRMED_RELEASE = re.compile(
    r"\b(?:minutes?|official data|report|statement)\s+(?:show|shows|say|says|confirm|confirms)\b|"
    r"\b(?:stands?|rose|rises|fell|falls|increased|decreased)\s+(?:at|to|by)\s+[-+]?\d",
    re.I,
)
_RECOVERY_EXECUTED_EVENT = re.compile(
    r"\b(?:launches?|launched|fires?|fired|strikes?|struck|attacks?|attacked|invades?|invaded|"
    r"imposes?|imposed|issues?|issued|halts?|halted|resumes?|resumed|suspends?|suspended|"
    r"cuts?|cut|raises?|raised|reopens?|reopened|closes?|closed|seizes?|seized|"
    r"approves?|approved|signs?|signed|agrees?|agreed|takes? effect)\b",
    re.I,
)
_CENTRAL_BANK_ACTOR = re.compile(
    r"\b(?:fed|fomc|federal reserve|boj|bank of japan|ecb|european central bank|"
    r"boe|bank of england|rbi|reserve bank of india)\b",
    re.I,
)
_CENTRAL_BANK_DECISION_REGION = (
    ("JP", re.compile(r"\bJapan\s+(?:Interest|Policy) Rate Decision\b", re.I)),
    ("EU", re.compile(r"\b(?:Eurozone|Euro Area)\s+(?:Interest|Policy) Rate Decision\b", re.I)),
    ("GB", re.compile(r"\b(?:UK|United Kingdom|Britain)\s+(?:Interest|Policy) Rate Decision\b", re.I)),
    ("IN", re.compile(r"\bIndia\s+(?:Interest|Policy|Repo) Rate Decision\b", re.I)),
    ("US", re.compile(r"\b(?:U\.S\.|US|United States)\s+(?:Interest|Policy|Fed Funds) Rate Decision\b", re.I)),
)
_CENTRAL_BANK_MINUTES = re.compile(
    r"\b(?:minutes|meeting minutes|meeting accounts?|meeting record)\b",
    re.I,
)
_CENTRAL_BANK_SPEECH_OR_ATTRIBUTION = re.compile(
    r"\b(?:says?|said|warns?|warned|argues?|argued|backs?|backed|advocates?|advocated|"
    r"favors?|favours?|favored|favoured|expects?|expected|calls? for|called for)\b",
    re.I,
)
_CENTRAL_BANK_OUTLOOK = re.compile(
    r"\b(?:could|would|should|must|likely|may|might|prudent|ready to|needs? to|outlook|"
    r"path|trajectory|future|next meeting|year[- ]end|data[- ]dependent|depends? on)\b",
    re.I,
)
_CENTRAL_BANK_OFFICIAL_DECISION = re.compile(
    r"\b(?:fed|fomc|federal reserve|boj|bank of japan|ecb|european central bank|"
    r"boe|bank of england|rbi|reserve bank of india)\b.{0,100}"
    r"\b(?:raises?|raised|cuts?|cut|holds?|held|maintains?|maintained|keeps?|kept|"
    r"leaves?|left)\b.{0,80}\b(?:target (?:interest )?rate|policy rate|interest rates?|rates?)\b|"
    r"\b(?:rate decision|interest rate decision|policy decision)\b.{0,80}"
    r"\b(?:raises?|raised|cuts?|cut|holds?|held|maintains?|maintained|keeps?|kept|"
    r"unchanged|basis points?|bps)\b|"
    r"\b(?:Japan|Eurozone|Euro Area|UK|United Kingdom|Britain|India|U\.S\.|US|United States)\s+"
    r"(?:Interest|Policy|Repo|Fed Funds) Rate Decision\s+"
    r"(?:Unch(?:anged|naged)|Raised|Increased|Hiked|Cut|Lowered|Reduced|Held|Maintained|At)\b|"
    r"\b(?:Japan|Eurozone|Euro Area|UK|United Kingdom|Britain|India|U\.S\.|US|United States)\s+"
    r"(?:Interest|Policy|Repo|Fed Funds) Rate Decision\s+[-+]?\d+(?:\.\d+)?%?\s+"
    r"(?:Vs\.?|Actual|Est(?:imate)?\.?).{0,80}\b(?:Est(?:imate)?\.?|Prior|Previous)\b",
    re.I,
)
_CENTRAL_BANK_INSTITUTION_CURRENT_DECISION = re.compile(
    r"\b(?P<subject>fomc|federal reserve|fed(?!['â€™]s)|boj|bank of japan|ecb|"
    r"european central bank|boe|bank of england|rbi|reserve bank of india)\b"
    r"\s+(?:(?:unexpectedly|unanimously|officially)\s+|(?:has|have)\s+)?"
    r"(?P<action>raises?|raised|hikes?|hiked|cuts?|cut|lowers?|lowered|holds?|held|"
    r"maintains?|maintained|keeps?|kept|leaves?|left)\b.{0,45}"
    r"\b(?:target (?:interest )?rate|policy rate|deposit rate|interest rates?|rates?)\b",
    re.I,
)
_CENTRAL_BANK_CALENDAR_RESULT = re.compile(
    r"\b(?:Japan|Eurozone|Euro Area|UK|United Kingdom|Britain|India|U\.S\.|US|United States)\s+"
    r"(?:Interest|Policy|Repo|Fed Funds) Rate Decision\b.{0,120}"
    r"(?:Unch(?:anged|naged)|Raised|Increased|Hiked|Cut|Lowered|Reduced|Held|Maintained|"
    r"At\s+[-+]?\d|[-+]?\d+(?:\.\d+)?%?\s+(?:Vs\.?|Actual|Est(?:imate)?\.?))",
    re.I,
)
_CENTRAL_BANK_EXTERNAL_COMMENTATOR = re.compile(
    r"\b(?:treasury secretary|president trump|trump|white house|senator|representative)\b"
    r".{0,120}\b(?:fed|fomc|federal reserve|boj|ecb|boe|rbi)\b",
    re.I,
)
_CENTRAL_BANK_OFFICIAL_SPEAKER = re.compile(
    r"\b(?:fed|fomc|federal reserve|boj|bank of japan|ecb|european central bank|"
    r"boe|bank of england|rbi|reserve bank of india)['â€™]s\s+"
    r"(?:[a-z][a-z.'-]*\s+){0,3}(?:says?|said|warns?|warned|advocates?|advocated|"
    r"favors?|favours?|expects?|expected|thinks?|believes?)\b",
    re.I,
)
_CENTRAL_BANK_NAMED_OFFICIAL = re.compile(
    r"\b(?:powell|waller|barkin|kashkari|collins|miran|cook|kugler|schmid|bowman|"
    r"goolsbee|williams|jefferson|barr|daly|logan|bostic|musalem)\b.{0,50}"
    r"\b(?:says?|said|warns?|warned|argues?|argued|advocates?|favou?rs?|expects?|"
    r"believes?|thinks?)\b",
    re.I,
)
_PRIOR_DECISION_CONNECTOR = re.compile(
    r"\b(?:even as|after|following|since|when|despite|last|prior|previous|earlier)\b",
    re.I,
)
_CENTRAL_BANK_CALENDAR_PREVIEW = re.compile(
    r"\b(?:ahead of|tomorrow|this week|next week|upcoming|scheduled|decision looms|"
    r"will announce|will decide|expected to announce)\b.{0,100}"
    r"\b(?:rate decision|interest rate|policy rate|central bank|fed|fomc|boj|ecb|boe|rbi)\b|"
    r"\b(?:rate decision|interest rate decision|policy decision)\b.{0,80}"
    r"\b(?:tomorrow|this week|next week|looms|upcoming|scheduled)\b|"
    r"\b(?:fed|fomc|boj|ecb|boe|rbi)\b.{0,80}"
    r"\b(?:decision|announcement)\b.{0,40}\b(?:tomorrow|this week|next week|looms|upcoming)\b|"
    r"\b(?:ahead of|before)\b.{0,60}\b(?:policy|rate|july|this week['â€™]s)?\s*meeting\b|"
    r"\bthis week['â€™]s\s+meeting\b",
    re.I,
)
_TRADE_EXPORT_ACTION = re.compile(
    r"\b(?:export controls?|export restrictions?|export ban|import ban|tariffs?|trade restrictions?|embargo)\b",
    re.I,
)
_SEMICONDUCTOR = re.compile(r"\b(?:semiconductor|semiconductors|chips?|advanced computing|ai accelerators?|smh|soxx)\b", re.I)
_SANCTIONS_ACTION = re.compile(r"\b(?:imposes?|issues?|tightens?|widens?|lifts?|removes?|eases?|extends?)\b.{0,80}\b(?:sanctions?|embargo|asset freeze)\b|\b(?:sanctions?|embargo|asset freeze)\b.{0,80}\b(?:imposed|issued|lifted|removed|eased|extended|tightened|widened)\b", re.I)
_CYBER_ACTION = re.compile(r"\b(?:ransomware|cyber ?attack|data breach|security breach|malware|hacked|compromised)\b", re.I)
_GRAIN_ACTION = re.compile(r"\b(?:wheat|corn|soybeans?|grain|crop)\b.{0,100}\b(?:drought|shortage|export ban|import ban|cuts?|raises?|output|yield|supply)\b", re.I)
_KOREA_SECURITY = re.compile(r"\b(?:north korea|pyongyang)\b.{0,100}\b(?:missile|nuclear|launches?|fires?|tests?|mobilizes?|sanctions?)\b", re.I)
_ISSUER_ONLY = re.compile(r"\b(?:tsmc|taiwan semiconductor)\b.{0,100}\b(?:earnings|revenue|52-week high|ai demand|shares?|stock)\b", re.I)

_TIGHTENING = re.compile(r"\b(?:raises? (?:interest )?rates?|rate hike|tightens?|imposes?|issues? sanctions?|bans?|restricts?|cuts? output|closes?|halts?|attacks?|strikes?|launches?|escalates?)\b", re.I)
_EASING = re.compile(r"\b(?:cuts? (?:interest )?rates?|rate cut|eases?|lifts?|removes?|raises? output|reopens?|resumes?|ceasefire|truce|de-escalates?)\b", re.I)

# V5 remains precision-first. These rules describe framing classes, never row IDs,
# providers, dates, outcomes, or symbols selected after an event.
_V5_MARKET_REACTION_FRAMING = re.compile(
    r"\b(?:dow jones|nasdaq|s&p 500|stock) futures\b.{0,180}\b(?:loom|react|reaction|rise|fall)\b|"
    r"\bhow will\b.{0,120}\b(?:futures|markets?|stocks?)\b.{0,40}\breact\b|"
    r"^\s*\*?\s*(?:precious metals?|stocks?|markets?|shares?)\s+(?:rise|fall|jump|drop|surge|slide)\b|"
    r"\bmarkets? (?:are|is) ['\"]?(?:selling|buying)\b|"
    r"\bstocks? (?:surge|rise|fall|drop)\b.{0,100}\b(?:hopes?|fears?|after|amid)\b|"
    r"\bmarkets? (?:rall(?:y|ies|ied)|rose|fell|surged|dropped)\b.{0,80}\b(?:after|following)\b",
    re.I,
)
_V5_OPINION_IMPACT_FRAMING = re.compile(
    r"\b(?:analyst|strategist|commentator|cramer|ives|caruso-cabrera)\b.{0,100}\b"
    r"(?:says?|warns?|calls?|expects?)\b|"
    r"\bcould be ['\"]?(?:most )?(?:bullish|bearish)\b|"
    r"\b(?:are|is) costing\b.{0,100}\bbillions?\b",
    re.I,
)
_V5_PREVIEW_OR_PROPOSAL = re.compile(
    r"\b(?:prepares?|preparing)\b.{0,120}\b(?:order|proposal|meeting|talks?)\b|"
    r"\b(?:start|begins?|opens?)\b.{0,60}\b(?:online )?meeting\b|"
    r"\bmeeting\b.{0,100}\b(?:discusses?|proposed|proposal|agenda)\b|"
    r"\bweighs?\b.{0,120}\b(?:importing|exporting|proposal|deal|action|measures?)\b|"
    r"\bwill speak\b.{0,120}\babout\b",
    re.I,
)
_V5_NON_MATERIAL_SECONDARY = re.compile(
    r"\bwarns? of\b.{0,100}\b(?:risks?|mistakes?)\b|"
    r"\bwas aware of\b.{0,100}\bplans? to\b|"
    r"\b(?:tells?|telling|cites?)\b.{0,80}\b(?:un|security council|representative)\b.{0,120}\b"
    r"(?:fulfilled|objective|assessment)\b|"
    r"\bin a statement\b.{0,240}\b(?:calls? on|offers? help|firmly believe)\b",
    re.I,
)

_PROXY_REGISTRY: dict[str, tuple[_ProxySpec, ...]] = {
    "oil_supply": (
        _ProxySpec("USO", "commodity", 0.98, 0.98, 0.90),
        _ProxySpec("XLE", "producer_equity", 0.88, 0.84, 0.80),
        _ProxySpec("XOP", "exploration_production", 0.84, 0.82, 0.78),
    ),
    "oil_demand": (
        _ProxySpec("USO", "commodity", 0.96, 0.96, 0.90),
        _ProxySpec("XLE", "producer_equity", 0.84, 0.82, 0.80),
        _ProxySpec("XOP", "exploration_production", 0.80, 0.78, 0.78),
    ),
    "defense_conflict": (
        _ProxySpec("ITA", "defense_equity", 0.95, 0.92, 0.78),
        _ProxySpec("XAR", "equal_weight_defense_equity", 0.88, 0.86, 0.76),
    ),
    "rates_policy": (
        _ProxySpec("IEF", "duration", 0.94, 0.94, 0.92),
        _ProxySpec("UUP", "currency", 0.76, 0.72, 0.75),
    ),
    "japan_rates_fx": (
        _ProxySpec("FXY", "currency", 0.97, 0.96, 0.98),
        _ProxySpec("EWJ", "country_equity", 0.76, 0.66, 0.98),
        _ProxySpec("DXJ", "currency_hedged_equity", 0.68, 0.60, 0.96),
    ),
    "euro_rates_fx": (
        _ProxySpec("FXE", "currency", 0.94, 0.92, 0.96),
        _ProxySpec("EZU", "country_equity", 0.74, 0.64, 0.92),
    ),
    "china_taiwan": (
        _ProxySpec("EWT", "country_equity", 0.92, 0.88, 0.98),
        _ProxySpec("FXI", "country_equity", 0.84, 0.80, 0.90),
    ),
    "semis_supply": (
        _ProxySpec("SMH", "semiconductor_equity", 0.96, 0.96, 0.90),
        _ProxySpec("SOXX", "semiconductor_equity", 0.93, 0.93, 0.88),
    ),
    "sanctions": (
        _ProxySpec("GLD", "inflation_hedge", 0.84, 0.78, 0.70),
        _ProxySpec("UUP", "currency", 0.76, 0.70, 0.70),
    ),
    "cybersecurity": (
        _ProxySpec("CIBR", "cybersecurity_equity", 0.94, 0.92, 0.80),
        _ProxySpec("HACK", "cybersecurity_equity", 0.88, 0.86, 0.78),
    ),
    "grain_supply": (
        _ProxySpec("DBA", "broad_agriculture", 0.90, 0.88, 0.80),
        _ProxySpec("WEAT", "wheat_commodity", 0.82, 0.84, 0.72),
        _ProxySpec("CORN", "corn_commodity", 0.80, 0.82, 0.72),
    ),
    "korea_risk": (
        _ProxySpec("EWY", "country_equity", 0.94, 0.90, 0.98),
        _ProxySpec("ITA", "defense_equity", 0.74, 0.76, 0.62),
    ),
}

_FORBIDDEN_RANKING_KEYS = re.compile(r"(?:outcome|return|mfe|mae|pnl|post_event|future|label)", re.I)
_ALLOWED_PRE_EVENT_KEYS = {
    "median_dollar_volume_score",
    "spread_quality",
    "intraday_trade_coverage",
    "stale_trade_quality",
    "market_session_overlap",
    "family_exposure",
    "beta_purity",
    "data_availability",
}
_REQUIRED_PRE_EVENT_KEYS = frozenset(_ALLOWED_PRE_EVENT_KEYS)


def _clip(value: float) -> float:
    return round(max(0.0, min(1.0, float(value))), 6)


def canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda item: sorted(item) if isinstance(item, (set, frozenset)) else str(item),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _v5_causal_refinement(text: str) -> tuple[bool, str]:
    checks = (
        (_V5_MARKET_REACTION_FRAMING, "market_or_price_reaction_framing"),
        (_V5_OPINION_IMPACT_FRAMING, "opinion_or_impact_analysis"),
        (_V5_PREVIEW_OR_PROPOSAL, "preview_meeting_or_proposal"),
        (_V5_NON_MATERIAL_SECONDARY, "non_material_or_secondary_commentary"),
    )
    for pattern, reason in checks:
        if pattern.search(text):
            return False, reason
    return True, ""


def _span(text: str, pattern: re.Pattern[str]) -> tuple[str, int, int]:
    match = pattern.search(text)
    if match is None:
        return "", -1, -1
    return match.group(0), match.start(), match.end()


def _semantic_clauses(text: str) -> tuple[tuple[str, int, int], ...]:
    clauses: list[tuple[str, int, int]] = []
    cursor = 0
    for match in re.finditer(r"\s*;\s*|\s+--\s+|\s+[\u2014\u2013]\s+", text):
        raw = text[cursor:match.start()]
        clause = raw.strip(" -:;")
        if clause:
            start = cursor + raw.find(clause)
            clauses.append((clause, start, start + len(clause)))
        cursor = match.end()
    raw = text[cursor:]
    clause = raw.strip(" -:;")
    if clause:
        start = cursor + raw.find(clause)
        clauses.append((clause, start, start + len(clause)))
    return tuple(clauses) or ((text, 0, len(text)),)


def detect_macro_release_clause(headline: str) -> MacroReleaseDetection:
    """Identify a direct macro result, not commentary that merely mentions data."""
    text = normalize_headline(headline)
    accepted: list[tuple[str, int, int, tuple[re.Match[str], ...]]] = []
    for clause, start, end in _semantic_clauses(text):
        indicators = tuple(_MACRO_INDICATOR.finditer(clause))
        if not indicators:
            continue
        fixed_release = bool(_FED_PREFERRED_GAUGE_RESULT.search(clause))
        provider_template = bool(_MACRO_PROVIDER_RESULT_TEMPLATE.search(clause))
        direct_subject_result = bool(_MACRO_DIRECT_SUBJECT_RESULT.search(clause))
        speaker_context = bool(_MACRO_RELEASE_SPEAKER_CONTEXT.search(clause)) and not fixed_release
        authority_is_release_subject = bool(_MACRO_RELEASE_AUTHORITY_SUBJECT.search(clause))
        if speaker_context and not authority_is_release_subject:
            continue
        if not (fixed_release or provider_template or direct_subject_result):
            continue
        accepted.append((clause, start, end, indicators))
    if not accepted:
        return MacroReleaseDetection(False)
    has_inflation = any(_INFLATION_INDICATOR.search(clause) for clause, _, _, _ in accepted)
    has_other_macro = any(
        not _INFLATION_INDICATOR.fullmatch(match.group(0))
        for _, _, _, indicators in accepted
        for match in indicators
    )
    event_type = "macro_data_release" if has_other_macro else "inflation_data_release"
    referenced = ("inflation_data_release",) if has_inflation and has_other_macro else ()
    first_start = min(row[1] for row in accepted)
    last_end = max(row[2] for row in accepted)
    return MacroReleaseDetection(
        True,
        event_type,
        text[first_start:last_end],
        first_start,
        last_end,
        referenced,
    )


def detect_central_bank_decision(headline: str) -> CentralBankDecisionDetection:
    """Require an institutional/committee subject and a current action in one clause."""
    text = normalize_headline(headline)
    if detect_macro_release_clause(text).is_release:
        return CentralBankDecisionDetection(False)
    for clause, _, _ in _semantic_clauses(text):
        calendar = _CENTRAL_BANK_CALENDAR_RESULT.search(clause)
        if calendar:
            return CentralBankDecisionDetection(True, "institution", "current_decision", calendar.group(0))
        action = _CENTRAL_BANK_INSTITUTION_CURRENT_DECISION.search(clause)
        if action:
            prefix = clause[:action.start()]
            nearby = clause[max(0, action.start() - 48):action.end() + 56]
            official_speaker_before = bool(
                _CENTRAL_BANK_OFFICIAL_SPEAKER.search(prefix)
                or _CENTRAL_BANK_NAMED_OFFICIAL.search(prefix)
                or _CENTRAL_BANK_SPEECH_OR_ATTRIBUTION.search(prefix)
            )
            external_before = bool(_CENTRAL_BANK_EXTERNAL_COMMENTATOR.search(prefix))
            prior_reference = bool(_PRIOR_DECISION_CONNECTOR.search(nearby))
            if official_speaker_before or external_before or prior_reference:
                subject_type = (
                    "external_commentator" if external_before
                    else "official_speaker" if official_speaker_before
                    else "institution"
                )
                return CentralBankDecisionDetection(
                    False,
                    subject_type,
                    "prior_decision_reference",
                    action.group(0),
                )
            subject = action.group("subject").lower()
            subject_type = "committee" if subject == "fomc" else "institution"
            return CentralBankDecisionDetection(True, subject_type, "current_decision", action.group(0))

    if _CENTRAL_BANK_EXTERNAL_COMMENTATOR.search(text):
        temporality = "hypothetical" if re.search(r"\b(?:would|could|if)\b", text, re.I) else "future_preference"
        return CentralBankDecisionDetection(False, "external_commentator", temporality)
    if _CENTRAL_BANK_OFFICIAL_SPEAKER.search(text) or _CENTRAL_BANK_NAMED_OFFICIAL.search(text) or (
        _CENTRAL_BANK_ACTOR.search(text) and _CENTRAL_BANK_SPEECH_OR_ATTRIBUTION.search(text)
    ):
        if re.search(r"\b(?:last|prior|previous)\b.{0,40}\b(?:decision|meeting|cut|hike)\b", text, re.I):
            temporality = "prior_decision_reference"
        elif re.search(r"\b(?:could|would|may|might|if)\b", text, re.I):
            temporality = "hypothetical"
        else:
            temporality = "future_preference"
        return CentralBankDecisionDetection(False, "official_speaker", temporality)
    return CentralBankDecisionDetection(False)


def _recovery_action_status(clause: str) -> str:
    if _RECOVERY_NON_MATERIAL.search(clause):
        return "conditional_or_commentary"
    macro = detect_macro_release_clause(clause)
    if macro.is_release or _RECOVERY_CONFIRMED_RELEASE.search(clause):
        return "confirmed_release"
    if _EMBEDDED_AUTHORITY_ACTION.search(clause):
        return "officially_announced"
    if _RECOVERY_EXECUTED_EVENT.search(clause) and (
        any(pattern.search(clause) for _, pattern in _ACTION_ACTOR_PATTERNS)
        or _MILITARY_ACTION.search(clause)
        or _CEASEFIRE_ACTION.search(clause)
    ):
        return "executed"
    return "unclear"


def _add_region(
    target: list[RegionEvidence],
    *,
    code: str,
    role: str,
    confidence: float,
    text: str,
    pattern: re.Pattern[str],
    relation_type: str = "contextual",
    action_status: str = "unclear",
) -> None:
    evidence, start, end = _span(text, pattern)
    if start < 0:
        return
    key = (code, role)
    if any((row.region_code, row.region_role) == key for row in target):
        return
    target.append(RegionEvidence(
        code,
        role,
        _clip(confidence),
        evidence,
        start,
        end,
        relation_type,
        action_status,
    ))


def resolve_region_roles_v5(headline: str, *, family_hint: str = "") -> tuple[tuple[RegionEvidence, ...], str, float, tuple[str, ...]]:
    text = normalize_headline(headline)
    regions: list[RegionEvidence] = []
    conflicts: list[str] = []

    central_bank_region = ""
    macro_detection = detect_macro_release_clause(text)
    macro_data_release = macro_detection.is_release
    if macro_data_release and _US_MACRO_DATA_CUE.search(text):
        regions.append(RegionEvidence(
            "US", "directly_affected", 0.97, macro_detection.evidence_text,
            macro_detection.evidence_start, macro_detection.evidence_end,
            "data_jurisdiction", "officially_announced",
        ))
        regions.append(RegionEvidence(
            "US", "market_expression", 0.94, macro_detection.evidence_text,
            macro_detection.evidence_start, macro_detection.evidence_end,
            "market_jurisdiction", "officially_announced",
        ))
    if not macro_data_release:
        for code, pattern in _CENTRAL_BANK_REGIONS:
            if pattern.search(text):
                central_bank_region = code
                _add_region(regions, code=code, role="actor", confidence=0.98, text=text, pattern=pattern,
                            relation_type="institutional_actor", action_status="unclear")
                _add_region(regions, code=code, role="directly_affected", confidence=0.97, text=text, pattern=pattern,
                            relation_type="policy_jurisdiction", action_status="unclear")
                _add_region(regions, code=code, role="market_expression", confidence=0.94, text=text, pattern=pattern,
                            relation_type="market_jurisdiction", action_status="unclear")
                break

    for code, pattern in _CENTRAL_BANK_DECISION_REGION:
        if pattern.search(text):
            central_bank_region = code
            _add_region(regions, code=code, role="directly_affected", confidence=0.98, text=text,
                        pattern=pattern, relation_type="policy_jurisdiction",
                        action_status="officially_announced")
            _add_region(regions, code=code, role="market_expression", confidence=0.96, text=text,
                        pattern=pattern, relation_type="market_jurisdiction",
                        action_status="officially_announced")
            break

    if _US_PROSPECTIVE_ACTOR.search(text):
        _add_region(
            regions,
            code="US",
            role="contextual",
            confidence=0.62,
            text=text,
            pattern=_US_PROSPECTIVE_ACTOR,
            relation_type="prospective_actor",
            action_status="conditional",
        )

    for code, pattern in _ACTION_ACTOR_PATTERNS:
        actor_match = pattern.search(text)
        prefix = text[max(0, actor_match.start() - 24):actor_match.start()] if actor_match else ""
        if actor_match and not _PROSPECTIVE_ACTOR_PREFIX.search(prefix) and not re.search(
            r"\b(?:says?|warns?|reports?|according|representative|official|source)\b",
            actor_match.group(0),
            re.I,
        ):
            base_pattern = next(item[1] for item in _REGION_PATTERNS if item[0] == code)
            status = "officially_announced" if re.search(r"\bannounces?\b", actor_match.group(0), re.I) else "executed"
            _add_region(regions, code=code, role="actor", confidence=0.96, text=text,
                        pattern=base_pattern, relation_type="subject_of_action",
                        action_status=status)

    for code, pattern in _TARGET_PATTERNS:
        if pattern.search(text):
            base_pattern = next(item[1] for item in _REGION_PATTERNS if item[0] == code)
            _add_region(regions, code=code, role="target", confidence=0.96, text=text,
                        pattern=base_pattern, relation_type="object_of_action",
                        action_status="executed")
            _add_region(regions, code=code, role="directly_affected", confidence=0.94, text=text,
                        pattern=base_pattern, relation_type="affected_by_action",
                        action_status="executed")

    if re.search(r"\b(?:drills?|military exercises?)\b.{0,80}\b(?:around|near)\s+Taiwan\b", text, re.I):
        cn_pattern = next(item[1] for item in _REGION_PATTERNS if item[0] == "CN")
        tw_pattern = next(item[1] for item in _REGION_PATTERNS if item[0] == "TW")
        _add_region(regions, code="CN", role="actor", confidence=0.98, text=text, pattern=cn_pattern)
        _add_region(regions, code="TW", role="target", confidence=0.98, text=text, pattern=tw_pattern)
        _add_region(regions, code="TW", role="directly_affected", confidence=0.97, text=text, pattern=tw_pattern)
        _add_region(regions, code="TW", role="market_expression", confidence=0.95, text=text, pattern=tw_pattern)

    if _SEMICONDUCTOR.search(text) and _TRADE_EXPORT_ACTION.search(text):
        semis_evidence, semis_start, semis_end = _span(text, _SEMICONDUCTOR)
        if re.search(r"\b(?:china|chinese|beijing)\b", text, re.I):
            cn_pattern = next(item[1] for item in _REGION_PATTERNS if item[0] == "CN")
            _add_region(regions, code="CN", role="target", confidence=0.95, text=text, pattern=cn_pattern)
            _add_region(regions, code="CN", role="directly_affected", confidence=0.92, text=text, pattern=cn_pattern)
        regions.append(RegionEvidence("TW", "supply_chain", 0.82, semis_evidence, semis_start, semis_end))
        regions.append(RegionEvidence("GLOBAL", "supply_chain", 0.86, semis_evidence, semis_start, semis_end))

    if _OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION.search(text):
        evidence, start, end = _span(text, _OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION)
        regions.append(RegionEvidence(
            "GLOBAL", "market_expression", 0.94, evidence, start, end,
            "market_jurisdiction", "officially_announced",
        ))
        regions.append(RegionEvidence(
            "GLOBAL", "directly_affected", 0.92, evidence, start, end,
            "forecast_scope", "officially_announced",
        ))
    elif _OIL_SUPPLY_ACTION.search(text):
        evidence, start, end = _span(text, _OIL_SUPPLY_ACTION)
        regions.append(RegionEvidence("GLOBAL", "market_expression", 0.91, evidence, start, end))
        regions.append(RegionEvidence("GLOBAL", "supply_chain", 0.92, evidence, start, end))

    if _ISSUER_ONLY.search(text) and not (_TRADE_EXPORT_ACTION.search(text) or _MILITARY_ACTION.search(text)):
        tw_pattern = next(item[1] for item in _REGION_PATTERNS if item[0] == "TW")
        _add_region(regions, code="TW", role="contextual", confidence=0.60, text=text, pattern=tw_pattern)

    used_codes = {row.region_code for row in regions}
    for code, pattern in _REGION_PATTERNS:
        if code in used_codes or not pattern.search(text):
            continue
        _add_region(regions, code=code, role="contextual", confidence=0.58, text=text, pattern=pattern)

    actors = {row.region_code for row in regions if row.region_role == "actor"}
    targets = {row.region_code for row in regions if row.region_role == "target"}
    if any(
        row.relation_type == "prospective_actor"
        or row.action_status in {"planned", "conditional"}
        for row in regions
    ):
        conflicts.append("conditional_or_prospective_action")
    if actors & targets:
        conflicts.append("actor_target_inversion")
    if _ISSUER_ONLY.search(text) and any(
        row.region_code == "TW" and row.region_role != "contextual" for row in regions
    ):
        conflicts.append("issuer_domicile_used_as_event_region")

    material = [row for row in regions if row.region_role not in {"contextual", "supply_chain"}]
    confidence = max((row.confidence for row in material), default=0.50)
    if actors and targets and not actors & targets:
        scope = "cross_border"
    elif any(row.region_code == "GLOBAL" for row in material):
        scope = "global"
    elif material:
        scope = "domestic_or_single_region"
    else:
        scope = "unclear"

    if family_hint in {"rates_policy", "japan_rates_fx", "euro_rates_fx"} and central_bank_region:
        market_regions = {row.region_code for row in regions if row.region_role == "market_expression"}
        if market_regions and central_bank_region not in market_regions:
            conflicts.append("critical_central_bank_country_mismatch")
    return tuple(regions), scope, _clip(confidence), tuple(sorted(set(conflicts)))


def _channel(
    code: str,
    role: str,
    direction: str,
    confidence: float,
    text: str,
    pattern: re.Pattern[str],
    rule_id: str,
) -> ChannelEvidence:
    evidence, _, _ = _span(text, pattern)
    return ChannelEvidence(code, role, direction, _clip(confidence), evidence, rule_id)


def construct_causal_channels_v5(headline: str) -> tuple[tuple[ChannelEvidence, ...], float]:
    text = normalize_headline(headline)
    channels: list[ChannelEvidence] = []
    tightening = bool(_TIGHTENING.search(text))
    easing = bool(_EASING.search(text))
    mixed = tightening and easing
    macro_detection = detect_macro_release_clause(text)
    macro_data_release = macro_detection.is_release
    official_energy_forecast = bool(_OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION.search(text))

    if macro_data_release:
        if macro_detection.event_type == "inflation_data_release":
            data_direction = (
                "increase" if re.search(r"\b(?:rises?|rose|increases?|increased|above|more than expected)\b", text, re.I)
                else "decrease" if re.search(r"\b(?:falls?|fell|decreases?|decreased|below|less than expected)\b", text, re.I)
                else "unclear"
            )
            channels.append(ChannelEvidence(
                "inflation_pressure", "primary", data_direction, 0.96,
                macro_detection.evidence_text, "v5.4-inflation-data-primary",
            ))
        else:
            channels.append(ChannelEvidence(
                "macro_activity", "primary", "unclear", 0.93,
                macro_detection.evidence_text, "v5.4-macro-data-primary",
            ))
    elif _RATE_POLICY_ACTION.search(text):
        mechanism = "unclear" if mixed or not (tightening or easing) else ("tightening" if tightening else "easing")
        channels.append(_channel("rates_discount_rate", "primary", mechanism, 0.96, text, _RATE_POLICY_ACTION, "v5-rates-policy-primary"))
        fx_direction = "unclear" if mechanism == "unclear" else ("increase" if mechanism == "tightening" else "decrease")
        channels.append(_channel("fx_currency", "secondary", fx_direction, 0.82, text, _RATE_POLICY_ACTION, "v5-rates-fx-secondary"))

    if official_energy_forecast:
        forecast_down = bool(re.search(r"\b(?:cuts?|cut|lowers?|lowered|reduces?|reduced)\b", text, re.I))
        forecast_up = bool(re.search(r"\b(?:raises?|raised|increases?|increased)\b", text, re.I))
        demand_direction = (
            "mixed" if forecast_down and forecast_up
            else "decrease" if forecast_down
            else "increase" if forecast_up
            else "unclear"
        )
        channels.append(_channel(
            "energy_demand", "primary", demand_direction, 0.97, text,
            _OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION,
            "v5.4-official-energy-demand-forecast-primary",
        ))
    elif _OIL_SUPPLY_ACTION.search(text):
        mechanism = "unclear" if mixed or not (tightening or easing) else ("decrease" if tightening else "increase")
        channels.append(_channel("energy_supply", "primary", mechanism, 0.97, text, _OIL_SUPPLY_ACTION, "v5-energy-supply-primary"))
        inflation_direction = "unclear" if mechanism == "unclear" else ("increase" if mechanism == "decrease" else "decrease")
        channels.append(_channel("inflation_pressure", "secondary", inflation_direction, 0.82, text, _OIL_SUPPLY_ACTION, "v5-energy-inflation-secondary"))

    if _TRADE_EXPORT_ACTION.search(text):
        mechanism = "unclear" if mixed or not (tightening or easing) else ("tightening" if tightening else "easing")
        channels.append(_channel("trade_export_control", "primary", mechanism, 0.95, text, _TRADE_EXPORT_ACTION, "v5-trade-export-primary"))
        if _SEMICONDUCTOR.search(text):
            semis_direction = "unclear" if mechanism == "unclear" else ("disruption" if mechanism == "tightening" else "normalization")
            channels.append(_channel("semiconductor_supply_chain", "secondary", semis_direction, 0.94, text, _SEMICONDUCTOR, "v5-semis-supply-secondary"))

    if _SANCTIONS_ACTION.search(text) and not _TRADE_EXPORT_ACTION.search(text):
        mechanism = "unclear" if mixed or not (tightening or easing) else ("tightening" if tightening else "easing")
        if re.search(r"\b(?:oil|crude|energy|tanker|refinery)\b", text, re.I):
            supply_direction = "unclear" if mechanism == "unclear" else ("decrease" if mechanism == "tightening" else "increase")
            channels.append(_channel("energy_supply", "primary", supply_direction, 0.95, text, _SANCTIONS_ACTION, "v5-energy-sanctions-primary"))
        else:
            channels.append(_channel("sovereign_risk", "primary", mechanism, 0.86, text, _SANCTIONS_ACTION, "v5-sovereign-sanctions-primary"))

    if _MILITARY_ACTION.search(text):
        channels.append(_channel("risk_off", "primary", "increase", 0.94, text, _MILITARY_ACTION, "v5-military-risk-primary"))
        channels.append(_channel("defense_demand", "secondary", "increase", 0.91, text, _MILITARY_ACTION, "v5-military-defense-secondary"))
    elif _CEASEFIRE_ACTION.search(text):
        channels.append(_channel("risk_off", "primary", "decrease", 0.92, text, _CEASEFIRE_ACTION, "v5-ceasefire-risk-primary"))
        channels.append(_channel("defense_demand", "secondary", "decrease", 0.78, text, _CEASEFIRE_ACTION, "v5-ceasefire-defense-secondary"))

    if _CYBER_ACTION.search(text):
        channels.append(_channel("cybersecurity_spending", "primary", "increase", 0.94, text, _CYBER_ACTION, "v5-cyber-spending-primary"))
        channels.append(_channel("regulatory_cost", "secondary", "increase", 0.79, text, _CYBER_ACTION, "v5-cyber-regulatory-secondary"))

    if _GRAIN_ACTION.search(text):
        direction = "decrease" if tightening else ("increase" if easing else "unclear")
        channels.append(_channel("commodity_supply", "primary", direction, 0.90, text, _GRAIN_ACTION, "v5-grain-supply-primary"))
        channels.append(_channel("inflation_pressure", "secondary", "increase" if direction == "decrease" else "unclear", 0.76, text, _GRAIN_ACTION, "v5-grain-inflation-secondary"))

    if _KOREA_SECURITY.search(text):
        channels.append(_channel("sovereign_risk", "primary", "increase", 0.93, text, _KOREA_SECURITY, "v5-korea-sovereign-primary"))
        channels.append(_channel("risk_off", "secondary", "increase", 0.88, text, _KOREA_SECURITY, "v5-korea-risk-secondary"))

    deduped: list[ChannelEvidence] = []
    for row in channels:
        existing = next((item for item in deduped if item.channel_code == row.channel_code and item.channel_role == row.channel_role), None)
        if existing is None or row.confidence > existing.confidence:
            if existing is not None:
                deduped.remove(existing)
            deduped.append(row)
    primary = [row.confidence for row in deduped if row.channel_role == "primary"]
    return tuple(deduped), _clip(max(primary, default=0.45))


def infer_event_types_v5(
    headline: str,
    news_content_type: str,
    channels: Sequence[ChannelEvidence],
) -> tuple[str, tuple[str, ...]]:
    """Infer a primary event and referenced events independently of proxy family."""
    text = normalize_headline(headline)
    primary = {row.channel_code for row in channels if row.channel_role == "primary"}
    all_channels = {row.channel_code for row in channels}
    macro_detection = detect_macro_release_clause(text)
    if macro_detection.is_release:
        return macro_detection.event_type, macro_detection.referenced_event_types
    if _OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION.search(text):
        return "official_energy_demand_forecast_revision", ()
    central_bank_context = bool(
        _CENTRAL_BANK_ACTOR.search(text)
        or _CENTRAL_BANK_NAMED_OFFICIAL.search(text)
        or any(pattern.search(text) for _, pattern in _CENTRAL_BANK_DECISION_REGION)
    )
    decision = detect_central_bank_decision(text)
    speaker_context = bool(
        _CENTRAL_BANK_OFFICIAL_SPEAKER.search(text)
        or _CENTRAL_BANK_NAMED_OFFICIAL.search(text)
        or _CENTRAL_BANK_SPEECH_OR_ATTRIBUTION.search(text)
        or decision.subject_type in {"official_speaker", "external_commentator"}
    )
    central_event = ""
    if central_bank_context and _CENTRAL_BANK_MINUTES.search(text):
        central_event = "central_bank_minutes"
    elif decision.is_current_decision:
        # An explicit observed result outranks a later press-conference quote.
        central_event = "central_bank_decision"
    elif central_bank_context and speaker_context:
        central_event = "central_bank_communication"
    elif central_bank_context and _CENTRAL_BANK_CALENDAR_PREVIEW.search(text):
        central_event = "central_bank_calendar_preview"
    elif central_bank_context and _CENTRAL_BANK_OUTLOOK.search(text):
        central_event = "central_bank_outlook_commentary"
    elif "rates_discount_rate" in primary:
        central_event = "central_bank_communication"

    contextual_primary = {
        "market_recap",
        "price_move_recap",
        "opinion",
        "technical_analysis",
        "listicle",
        "valuation_article",
    }
    if decision.temporality == "prior_decision_reference" and not central_event:
        if news_content_type in contextual_primary:
            return news_content_type, ("central_bank_decision",)
        central_event = "central_bank_communication"
    if central_event:
        if news_content_type in contextual_primary:
            contextual_references = [central_event]
            if decision.temporality == "prior_decision_reference":
                contextual_references.append("central_bank_decision")
            return news_content_type, tuple(dict.fromkeys(contextual_references))
        referenced: list[str] = []
        if (
            central_event != "central_bank_communication"
            and _CENTRAL_BANK_SPEECH_OR_ATTRIBUTION.search(text)
        ):
            referenced.append("central_bank_communication")
        if (
            central_event != "central_bank_outlook_commentary"
            and _CENTRAL_BANK_OUTLOOK.search(text)
        ):
            referenced.append("central_bank_outlook_commentary")
        if decision.temporality == "prior_decision_reference":
            referenced.append("central_bank_decision")
        if central_event == "central_bank_communication" and _CENTRAL_BANK_CALENDAR_PREVIEW.search(text):
            referenced.append("central_bank_calendar_preview")
        if _INFLATION_INDICATOR.search(text) and central_event != "inflation_data_release":
            referenced.append("inflation_data_release")
        elif _MACRO_INDICATOR.search(text) and central_event != "macro_data_release":
            referenced.append("macro_data_release")
        return central_event, tuple(dict.fromkeys(referenced))
    if "energy_supply" in primary:
        return "energy_supply_event", ()
    if "trade_export_control" in primary:
        return "trade_or_cross_strait_policy_action", ()
    if "risk_off" in primary:
        return "geopolitical_conflict_event", ()
    if "cybersecurity_spending" in primary:
        return "cybersecurity_incident", ()
    if "commodity_supply" in primary:
        return "agricultural_supply_event", ()
    if "sovereign_risk" in primary and _KOREA_SECURITY.search(text):
        return "geopolitical_security_event", ()
    if _SANCTIONS_ACTION.search(text) and "energy_supply" not in all_channels:
        return "sanctions_policy_action", ()
    return news_content_type, ()


def infer_event_type_v5(
    headline: str,
    news_content_type: str,
    channels: Sequence[ChannelEvidence],
) -> str:
    """Compatibility wrapper returning only the primary event type."""
    return infer_event_types_v5(headline, news_content_type, channels)[0]


def recover_event_clause_v5(
    headline: str,
    *,
    provider: str = "",
) -> tuple[bool, str, str, str]:
    """Recover a new-event clause without promoting it to strict causal status."""
    text = normalize_headline(headline)
    # Canonical Unicode-aware split supersedes the legacy dash-compatible split below.
    clauses = [part.strip(" -:;") for part in re.split(r"\s*;\s*|\s+--\s+|\s+[—–]\s+", text) if part.strip()]
    if len(clauses) < 2:
        clauses = [part[0] for part in _semantic_clauses(text)]
    if len(clauses) < 2:
        for connector in re.finditer(r"\s+\b(?:as|after|amid|following)\b\s+", text, re.I):
            framing = text[:connector.start()].strip(" -:;")
            candidate = text[connector.end():].strip(" -:;")
            if (
                framing
                and candidate
                and _EMBEDDED_AUTHORITY_ACTION.search(candidate)
                and _recovery_action_status(candidate)
                in {"officially_announced", "executed", "confirmed_release"}
            ):
                clauses = [framing, candidate]
                break
    if len(clauses) < 2:
        return False, "", "", "not_applicable"
    framing_clause = ""
    event_clause = ""
    event_status = "not_applicable"
    for clause in clauses:
        base = classify_v4_causal_proxy_headline(clause, provider=provider)
        embedded_material = bool(_EMBEDDED_AUTHORITY_ACTION.search(clause))
        action_status = _recovery_action_status(clause)
        refined, _ = (
            _v5_causal_refinement(clause)
            if base.eligible or embedded_material
            else (False, "")
        )
        channels, _ = construct_causal_channels_v5(clause)
        if (
            action_status in {"executed", "officially_announced", "confirmed_release"}
            and (base.eligible or embedded_material)
            and refined
            and any(row.channel_role == "primary" for row in channels)
        ):
            event_clause = clause
            event_status = action_status
        elif not framing_clause:
            framing_clause = clause
    recovered = bool(event_clause and framing_clause)
    return (
        recovered,
        event_clause if recovered else "",
        framing_clause if recovered else "",
        event_status if recovered else "not_applicable",
    )


def _market_region(regions: Sequence[RegionEvidence]) -> str:
    for role in ("market_expression", "directly_affected", "target", "actor"):
        candidates = [row for row in regions if row.region_role == role]
        if candidates:
            return max(candidates, key=lambda row: row.confidence).region_code
    return "GLOBAL"


def generate_family_candidates_v5(
    headline: str,
    regions: Sequence[RegionEvidence],
    channels: Sequence[ChannelEvidence],
    *,
    causal_eligible: bool,
) -> tuple[FamilyCandidate, ...]:
    if not causal_eligible:
        return ()
    text = normalize_headline(headline)
    primary_channels = {row.channel_code: row for row in channels if row.channel_role == "primary"}
    all_channels = {row.channel_code: row for row in channels}
    market_region = _market_region(regions)
    candidates: list[FamilyCandidate] = []

    def add(family: str, confidence: float, passed: bool, evidence: str, rule: str) -> None:
        candidates.append(FamilyCandidate(family, _clip(confidence), passed, evidence, rule))

    if "rates_discount_rate" in primary_channels:
        family = "japan_rates_fx" if market_region == "JP" else ("euro_rates_fx" if market_region == "EU" else "rates_policy")
        add(family, 0.96, bool(_RATE_POLICY_ACTION.search(text)), primary_channels["rates_discount_rate"].evidence_text, "v5-rates-hard-evidence")
    if "energy_supply" in primary_channels:
        add("oil_supply", 0.96, bool(_OIL_SUPPLY_ACTION.search(text) or _SANCTIONS_ACTION.search(text)), primary_channels["energy_supply"].evidence_text, "v5-oil-supply-hard-evidence")
    if "energy_demand" in primary_channels:
        add(
            "oil_demand",
            0.95,
            bool(_OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION.search(text)),
            primary_channels["energy_demand"].evidence_text,
            "v5.4-official-oil-demand-forecast-hard-evidence",
        )
    if "semiconductor_supply_chain" in all_channels:
        add("semis_supply", 0.95, bool(_SEMICONDUCTOR.search(text) and _TRADE_EXPORT_ACTION.search(text)), all_channels["semiconductor_supply_chain"].evidence_text, "v5-semis-hard-evidence")
    if "trade_export_control" in primary_channels and market_region in {"CN", "TW"}:
        add("china_taiwan", 0.88, bool(_TRADE_EXPORT_ACTION.search(text)), primary_channels["trade_export_control"].evidence_text, "v5-china-policy-hard-evidence")
    target_regions = {row.region_code for row in regions if row.region_role == "target"}
    if "TW" in target_regions and _MILITARY_ACTION.search(text):
        add("china_taiwan", 0.98, True, _MILITARY_ACTION.search(text).group(0), "v5-taiwan-direct-security-hard-evidence")
    if "risk_off" in primary_channels and (_MILITARY_ACTION.search(text) or _CEASEFIRE_ACTION.search(text)):
        evidence = primary_channels["risk_off"].evidence_text
        confidence = 0.88 if "TW" in target_regions else 0.93
        add("defense_conflict", confidence, True, evidence, "v5-defense-action-hard-evidence")
    if "cybersecurity_spending" in primary_channels:
        add("cybersecurity", 0.94, bool(_CYBER_ACTION.search(text)), primary_channels["cybersecurity_spending"].evidence_text, "v5-cyber-incident-hard-evidence")
    if "commodity_supply" in primary_channels:
        add("grain_supply", 0.91, bool(_GRAIN_ACTION.search(text)), primary_channels["commodity_supply"].evidence_text, "v5-grain-supply-hard-evidence")
    if _KOREA_SECURITY.search(text):
        add("korea_risk", 0.93, True, _KOREA_SECURITY.search(text).group(0), "v5-korea-security-hard-evidence")
    if _SANCTIONS_ACTION.search(text) and not {"energy_supply", "semiconductor_supply_chain"} & set(all_channels):
        add("sanctions", 0.86, True, _SANCTIONS_ACTION.search(text).group(0), "v5-sanctions-sovereign-hard-evidence")

    best_by_family: dict[str, FamilyCandidate] = {}
    for row in candidates:
        if row.proxy_family not in best_by_family or row.confidence > best_by_family[row.proxy_family].confidence:
            best_by_family[row.proxy_family] = row
    return tuple(sorted(best_by_family.values(), key=lambda row: (-row.confidence, row.proxy_family))[:3])


def select_family_v5(
    candidates: Sequence[FamilyCandidate],
    thresholds: V5Thresholds,
) -> tuple[str, float, bool, str]:
    valid = [row for row in candidates if row.required_evidence_pass]
    if not valid:
        return "", 0.0, True, "no_family_with_required_evidence"
    ordered = sorted(valid, key=lambda row: (-row.confidence, row.proxy_family))
    top = ordered[0]
    gap = top.confidence - ordered[1].confidence if len(ordered) > 1 else top.confidence
    if top.confidence < thresholds.family_minimum:
        return "", top.confidence, True, "family_confidence_below_minimum"
    if len(ordered) > 1 and gap < 0.05:
        return "", top.confidence, True, "family_top1_top2_margin_too_small"
    return top.proxy_family, top.confidence, False, ""


def _validate_pre_event_metrics(metrics: Mapping[str, Mapping[str, float]] | None) -> None:
    if metrics is None:
        return
    for symbol, values in metrics.items():
        if not isinstance(values, Mapping):
            raise ValueError(f"pre-event metrics for {symbol} must be a mapping")
        for key, raw_value in values.items():
            if _FORBIDDEN_RANKING_KEYS.search(key):
                raise ValueError(f"post-event/outcome ranking feature is forbidden: {key}")
            if key not in _ALLOWED_PRE_EVENT_KEYS:
                raise ValueError(f"unsupported pre-event ranking feature: {key}")
            value = float(raw_value)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"pre-event metric must be in [0,1]: {symbol}.{key}")


def _expected_proxy_direction(
    symbol: str,
    family: str,
    event_direction: str,
    channels: Sequence[ChannelEvidence],
) -> tuple[str, float]:
    primary = next((row for row in channels if row.channel_role == "primary"), None)
    mechanism = primary.mechanism_direction if primary else "unclear"
    if event_direction in {"mixed", "unclear"} or mechanism in {"mixed", "unclear"}:
        return ("mixed" if event_direction == "mixed" else "unknown"), 0.45
    if family == "oil_supply":
        return ("positive" if mechanism == "decrease" else "negative"), 0.86
    if family == "oil_demand":
        return ("negative" if mechanism == "decrease" else "positive"), 0.82
    if family in {"japan_rates_fx", "euro_rates_fx"}:
        if symbol in {"FXY", "FXE"}:
            return ("positive" if event_direction == "tightening" else "negative"), 0.82
        return "mixed", 0.50
    if family == "rates_policy":
        if symbol == "IEF":
            return ("negative" if event_direction == "tightening" else "positive"), 0.88
        return ("positive" if event_direction == "tightening" else "negative"), 0.75
    if family in {"defense_conflict", "korea_risk"}:
        return ("positive" if event_direction == "escalation" else "negative"), 0.78
    if family in {"china_taiwan", "semis_supply"}:
        return ("negative" if mechanism in {"tightening", "disruption", "increase"} else "positive"), 0.77
    if family in {"cybersecurity", "sanctions"}:
        return "mixed", 0.52
    if family == "grain_supply":
        return ("positive" if mechanism == "decrease" else "negative"), 0.76
    return "unknown", 0.40


def rank_proxy_candidates_v5(
    family: str,
    family_confidence: float,
    event_direction: str,
    regions: Sequence[RegionEvidence],
    channels: Sequence[ChannelEvidence],
    *,
    pre_event_metrics: Mapping[str, Mapping[str, float]] | None = None,
    pre_event_feature_snapshot_id: str = "",
    thresholds: V5Thresholds | None = None,
) -> tuple[tuple[ProxyCandidate, ...], str, float, float, bool, str]:
    thresholds = thresholds or V5Thresholds()
    _validate_pre_event_metrics(pre_event_metrics)
    specs = _PROXY_REGISTRY.get(family, ())
    if not specs:
        return (), "", 0.0, 0.0, True, "no_proxy_registry_for_family"
    metrics = pre_event_metrics or {}
    metrics_complete = bool(pre_event_metrics)
    if pre_event_metrics is not None:
        for spec in specs:
            values = pre_event_metrics.get(spec.symbol)
            if values is None or set(values) != _REQUIRED_PRE_EVENT_KEYS:
                metrics_complete = False
                break
    rows: list[ProxyCandidate] = []
    for spec in specs:
        values = metrics.get(spec.symbol, {})
        liquidity = float(values.get("median_dollar_volume_score", 0.50))
        spread = float(values.get("spread_quality", 0.50))
        intraday = float(values.get("intraday_trade_coverage", 0.50))
        stale = float(values.get("stale_trade_quality", 0.50))
        session = float(values.get("market_session_overlap", 0.50))
        exposure = float(values.get("family_exposure", 0.50))
        purity = float(values.get("beta_purity", 0.50))
        availability = float(values.get("data_availability", 0.50))
        score = (
            0.25 * spec.semantic_relevance
            + 0.17 * spec.channel_specificity
            + 0.13 * spec.region_specificity
            + 0.08 * liquidity
            + 0.08 * spread
            + 0.05 * intraday
            + 0.05 * stale
            + 0.05 * session
            + 0.06 * exposure
            + 0.04 * purity
            + 0.04 * availability
        )
        expected, expected_confidence = _expected_proxy_direction(
            spec.symbol, family, event_direction, channels
        )
        rows.append(ProxyCandidate(
            family,
            spec.symbol,
            "primary_candidate",
            spec.expression_type,
            spec.semantic_relevance,
            spec.region_specificity,
            spec.channel_specificity,
            _clip((liquidity + spread + intraday + stale + session) / 5.0),
            _clip(spread),
            _clip(intraday),
            _clip(stale),
            _clip(session),
            _clip(exposure),
            _clip(purity),
            _clip(availability),
            _clip(score),
            0,
            expected,
            _clip(expected_confidence),
        ))
    ordered = sorted(rows, key=lambda row: (-row.final_score, row.symbol))
    ranked = tuple(
        ProxyCandidate(**{**asdict(row), "proxy_role": "primary_candidate" if index == 1 else "secondary_observation", "rank": index})
        for index, row in enumerate(ordered, 1)
    )
    top = ranked[0]
    gap = top.final_score - ranked[1].final_score if len(ranked) > 1 else top.final_score
    abstain_reason = ""
    if pre_event_metrics is None:
        abstain_reason = "missing_pre_event_metrics"
    elif not pre_event_feature_snapshot_id:
        abstain_reason = "missing_pre_event_feature_snapshot_id"
    elif not metrics_complete:
        abstain_reason = "incomplete_pre_event_metrics"
    elif top.final_score < thresholds.proxy_score_minimum:
        abstain_reason = "top1_score_below_minimum"
    elif len(ranked) > 1 and gap < thresholds.proxy_top1_top2_margin:
        abstain_reason = "top1_top2_margin_too_small"
    elif family_confidence < thresholds.family_minimum:
        abstain_reason = "semantic_family_confidence_below_minimum"
    confidence = _clip(
        0.45 * family_confidence
        + 0.30 * top.semantic_relevance
        + 0.15 * top.final_score
        + 0.10 * min(1.0, gap / 0.10)
    )
    return ranked, "" if abstain_reason else top.symbol, confidence, _clip(gap), bool(abstain_reason), abstain_reason


def _event_direction(
    headline: str,
    family: str,
    channels: Sequence[ChannelEvidence],
) -> tuple[str, float, str]:
    text = normalize_headline(headline)
    primary = next((row for row in channels if row.channel_role == "primary"), None)
    direction = "unclear"
    if primary is not None:
        if primary.channel_code in {"rates_discount_rate", "trade_export_control", "sovereign_risk"}:
            if primary.mechanism_direction in {"tightening", "easing"}:
                direction = primary.mechanism_direction
        elif primary.channel_code == "risk_off":
            if primary.mechanism_direction == "increase":
                direction = "escalation"
            elif primary.mechanism_direction == "decrease":
                direction = "deescalation"
        elif primary.channel_code in {"energy_supply", "commodity_supply"}:
            if primary.mechanism_direction == "decrease":
                direction = "tightening"
            elif primary.mechanism_direction == "increase":
                direction = "easing"
        elif primary.channel_code == "energy_demand":
            if primary.mechanism_direction == "decrease":
                direction = "demand_outlook_down"
            elif primary.mechanism_direction == "increase":
                direction = "demand_outlook_up"
            elif primary.mechanism_direction == "mixed":
                direction = "mixed"
    legacy_family = {
        "japan_rates_fx": "rates_policy",
        "euro_rates_fx": "rates_policy",
        "semis_supply": "china_taiwan",
    }.get(family, family or "defense_conflict")
    if direction == "unclear":
        try:
            direction = infer_direction(legacy_family, text)
        except (KeyError, ValueError):
            direction = "unclear"
    tightening_match = _TIGHTENING.search(text)
    easing_match = _EASING.search(text)
    military_match = _MILITARY_ACTION.search(text)
    ceasefire_match = _CEASEFIRE_ACTION.search(text)
    if tightening_match and easing_match:
        direction = "mixed"
    elif direction == "unclear" and tightening_match:
        direction = "tightening"
    elif direction == "unclear" and easing_match:
        direction = "easing"
    elif direction == "unclear" and military_match:
        direction = "escalation"
    elif direction == "unclear" and ceasefire_match:
        direction = "deescalation"
    explicit = tightening_match or easing_match or military_match or ceasefire_match
    rule_strength = 0.94 if explicit and direction not in {"mixed", "unclear"} else (0.55 if direction == "mixed" else 0.38)
    evidence = explicit.group(0) if explicit else ""
    return direction, _clip(rule_strength), evidence


def _build_directions(
    event_direction: str,
    event_rule_strength: float,
    event_empirical_precision: float | None,
    event_calibrated_confidence: float | None,
    event_evidence: str,
    channels: Sequence[ChannelEvidence],
    proxy_candidates: Sequence[ProxyCandidate],
) -> tuple[DirectionEvidence, ...]:
    rows = [DirectionEvidence(
        "event",
        "event",
        event_direction,
        float(event_calibrated_confidence or 0.0),
        event_evidence,
        event_rule_strength,
        event_empirical_precision,
        event_calibrated_confidence,
    )]
    rows.extend(
        DirectionEvidence(
            "channel", row.channel_code, row.mechanism_direction, 0.0,
            row.evidence_text, row.confidence, None, None
        )
        for row in channels
    )
    rows.extend(
        DirectionEvidence(
            "proxy", row.symbol, row.expected_direction, 0.0,
            row.proxy_family, row.expected_direction_confidence, None, None
        )
        for row in proxy_candidates
    )
    return tuple(rows)


def _mapping_tier(
    *,
    causal_eligible: bool,
    recovered_event_candidate: bool,
    semantic_mapping_strict: bool,
    proxy_abstained: bool,
    directional_strict: bool,
    region_confidence: float,
    channel_confidence: float,
) -> str:
    if not causal_eligible:
        if recovered_event_candidate:
            return "D_recovered_event_candidate"
        return "E_context_or_negative_control"
    if semantic_mapping_strict and not proxy_abstained and directional_strict:
        return "A_strict_directional"
    if semantic_mapping_strict and proxy_abstained:
        return "B_proxy_basket_observation"
    if semantic_mapping_strict:
        return "C_non_directional_observation"
    if min(region_confidence, channel_confidence) < 0.70:
        return "D_core_only"
    return "C_non_directional_observation"


def map_headline_v5(
    headline: str,
    *,
    provider: str = "",
    pre_event_metrics: Mapping[str, Mapping[str, float]] | None = None,
    pre_event_feature_snapshot_id: str = "",
    learned_proxy_direction: str = "",
    learned_direction_confidence: float = 0.0,
    learned_direction_snapshot_id: str = "",
    event_direction_empirical_precision: float | None = None,
    event_direction_calibrated_confidence: float | None = None,
    direction_calibration_snapshot_id: str = "",
    market_data_quality_pass: bool = False,
    liquidity_pass: bool = False,
    policy_gate_pass: bool = False,
    human_adjudicated_news_content_type: str = "",
    content_type_adjudication_id: str = "",
    content_type_conflicting: bool = False,
    taxonomy_adjudication_id: str = "",
    taxonomy_expansion_status_override: str = "",
    thresholds: V5Thresholds | None = None,
    policy_config: V5PolicyConfig | None = None,
) -> V5SemanticMapping:
    if policy_config is not None and thresholds is not None and policy_config.thresholds != thresholds:
        raise ValueError("thresholds and policy_config.thresholds disagree")
    policy_config = policy_config or V5PolicyConfig(
        thresholds=thresholds or V5Thresholds()
    )
    thresholds = policy_config.thresholds
    text = normalize_headline(headline)
    macro_detection = detect_macro_release_clause(text)
    decision_detection = detect_central_bank_decision(text)
    official_energy_forecast = bool(_OFFICIAL_ENERGY_DEMAND_FORECAST_REVISION.search(text))
    causal = classify_v4_causal_proxy_headline(text, provider=provider)
    causal_eligible = causal.eligible
    causal_exclusion_reason = causal.exclusion_reason
    if official_energy_forecast:
        causal_eligible = True
        causal_exclusion_reason = ""
    if causal_eligible:
        causal_eligible, v5_reason = _v5_causal_refinement(text)
        if not causal_eligible:
            causal_exclusion_reason = v5_reason
    recovered_event_candidate = False
    recovered_event_clause = ""
    framing_clause = ""
    recovery_action_status = "not_applicable"
    if not causal_eligible:
        (
            recovered_event_candidate,
            recovered_event_clause,
            framing_clause,
            recovery_action_status,
        ) = recover_event_clause_v5(text, provider=provider)
    core_event_observation_eligible = recovered_event_candidate
    v4_rule = match_proxy_rule_v4(text, provider=provider) if causal_eligible else None
    family_hint = v4_rule.family if v4_rule is not None else ""
    regions, region_scope, region_confidence, region_conflicts = resolve_region_roles_v5(
        text, family_hint=family_hint
    )
    channels, channel_confidence = construct_causal_channels_v5(text)
    family_candidates = generate_family_candidates_v5(
        text, regions, channels, causal_eligible=causal_eligible
    )
    family, family_confidence, family_abstained, family_reason = select_family_v5(
        family_candidates, thresholds
    )
    event_direction, event_direction_rule_strength, event_evidence = _event_direction(
        text, family, channels
    )
    proxy_candidates, selected_proxy, proxy_confidence, proxy_gap, proxy_abstained, proxy_reason = rank_proxy_candidates_v5(
        family,
        family_confidence,
        event_direction,
        regions,
        channels,
        pre_event_metrics=pre_event_metrics,
        pre_event_feature_snapshot_id=pre_event_feature_snapshot_id,
        thresholds=thresholds,
    ) if family else ((), "", 0.0, 0.0, True, "proxy_family_abstained")

    conflicts = list(region_conflicts)
    market_region = _market_region(regions)
    if family == "japan_rates_fx" and market_region != "JP":
        conflicts.append("critical_japan_proxy_country_mismatch")
    if family == "euro_rates_fx" and market_region != "EU":
        conflicts.append("critical_euro_proxy_country_mismatch")
    if family == "rates_policy" and market_region in {"JP", "EU"}:
        conflicts.append("critical_rates_proxy_country_mismatch")
    critical_conflicts = tuple(sorted(set(conflicts)))
    family_watch_only = family in policy_config.small_sample_families_watch_only

    primary_channels = [row for row in channels if row.channel_role == "primary"]
    channel_direction_consistent = bool(primary_channels) and all(
        row.mechanism_direction not in {"mixed", "unclear"} for row in primary_channels
    )
    selected_candidate = next((row for row in proxy_candidates if row.symbol == selected_proxy), None)
    proxy_direction = selected_candidate.expected_direction if selected_candidate else "unknown"
    proxy_direction_unambiguous = proxy_direction not in {"mixed", "unknown", ""}
    learned_agrees = bool(learned_proxy_direction) and learned_proxy_direction == proxy_direction
    learned_confident = bool(learned_direction_snapshot_id) and (
        float(learned_direction_confidence) >= thresholds.learned_direction
    )
    if event_direction_calibrated_confidence is not None and not direction_calibration_snapshot_id:
        raise ValueError("direction calibration confidence requires a snapshot id")
    event_direction_confidence = _clip(event_direction_calibrated_confidence or 0.0)
    event_direction_empirical_precision = (
        None if event_direction_empirical_precision is None
        else _clip(event_direction_empirical_precision)
    )

    classified_content = classify_headline(text).news_content_type
    v5_validated_content = False
    if macro_detection.is_release:
        classified_content = "macro_data_release"
        v5_validated_content = True
    elif official_energy_forecast:
        classified_content = "official_forecast_revision"
        v5_validated_content = True
    elif _V5_MARKET_REACTION_FRAMING.search(text):
        classified_content = "market_recap"
    if content_type_conflicting:
        news_content_type = human_adjudicated_news_content_type or classified_content
        content_type_status = "conflicting"
        content_type_strict_eligible = False
    elif human_adjudicated_news_content_type:
        if not content_type_adjudication_id:
            raise ValueError("human-adjudicated content type requires an adjudication id")
        news_content_type = human_adjudicated_news_content_type
        content_type_status = "human_adjudicated"
        content_type_strict_eligible = news_content_type not in {"", "unknown", "conflicting"}
    else:
        if content_type_adjudication_id:
            raise ValueError("content type adjudication id requires an adjudicated value")
        news_content_type = classified_content
        if v5_validated_content:
            content_type_status = "v5_validated_rule"
        elif news_content_type != "unknown":
            content_type_status = "legacy_rule_unvalidated"
        else:
            content_type_status = "unknown"
        content_type_strict_eligible = content_type_status == "v5_validated_rule"
    event_type, referenced_event_types = infer_event_types_v5(
        text, news_content_type, channels
    )

    taxonomy_expansion_requires_adjudication = official_energy_forecast
    if taxonomy_expansion_status_override not in {"", "human_approved", "rejected"}:
        raise ValueError("unsupported taxonomy expansion status override")
    if not taxonomy_expansion_requires_adjudication:
        if taxonomy_adjudication_id or taxonomy_expansion_status_override:
            raise ValueError("taxonomy adjudication is only valid for an expansion candidate")
        taxonomy_expansion_status = "not_applicable"
    elif bool(taxonomy_adjudication_id) != bool(taxonomy_expansion_status_override):
        raise ValueError(
            "taxonomy adjudication id and explicit status override must be provided together"
        )
    elif taxonomy_adjudication_id and taxonomy_expansion_status_override:
        taxonomy_expansion_status = taxonomy_expansion_status_override
    else:
        taxonomy_expansion_status = "pending_adjudication"
    taxonomy_strict_eligible = (
        not taxonomy_expansion_requires_adjudication
        or taxonomy_expansion_status == "human_approved"
    )
    if taxonomy_expansion_status == "pending_adjudication":
        sample_purpose = "taxonomy_expansion_research"
    elif taxonomy_expansion_status == "rejected":
        sample_purpose = "taxonomy_rejected_context"
    else:
        sample_purpose = "standard_semantic_research"

    semantic_mapping_strict = bool(
        causal_eligible
        and content_type_strict_eligible
        and taxonomy_strict_eligible
        and not family_abstained
        and region_confidence >= thresholds.region
        and channel_confidence >= thresholds.channel
        and family_confidence >= thresholds.proxy_family
        and not critical_conflicts
    )
    directional_strict = bool(
        semantic_mapping_strict
        and event_direction_confidence >= thresholds.event_direction
        and channel_direction_consistent
        and proxy_direction_unambiguous
        and (
            not policy_config.safety.direction_requires_learned_sign_agreement
            or (learned_agrees and learned_confident)
        )
    )
    reaction_observation_eligible = bool(
        causal_eligible
        and not family_abstained
        and not critical_conflicts
        and taxonomy_expansion_status != "rejected"
        and min(region_confidence, channel_confidence, family_confidence) >= 0.65
    )
    trade_candidate_eligible = bool(
        semantic_mapping_strict
        and not proxy_abstained
        and proxy_confidence >= thresholds.primary_proxy
        and not family_watch_only
        and market_data_quality_pass
        and liquidity_pass
        and policy_gate_pass
    )
    directional_trade_candidate_eligible = bool(
        trade_candidate_eligible and directional_strict
    )
    strict_training_eligible = semantic_mapping_strict and taxonomy_strict_eligible

    directions = _build_directions(
        event_direction,
        event_direction_rule_strength,
        event_direction_empirical_precision,
        event_direction_calibrated_confidence,
        event_evidence,
        channels,
        proxy_candidates,
    )
    tier = _mapping_tier(
        causal_eligible=causal_eligible,
        recovered_event_candidate=recovered_event_candidate,
        semantic_mapping_strict=semantic_mapping_strict,
        proxy_abstained=proxy_abstained,
        directional_strict=directional_strict,
        region_confidence=region_confidence,
        channel_confidence=channel_confidence,
    )
    semantic_input_hash = canonical_hash({
        "normalized_headline": text,
        "provider": provider,
        "human_adjudicated_news_content_type": human_adjudicated_news_content_type,
        "content_type_adjudication_id": content_type_adjudication_id,
        "content_type_conflicting": bool(content_type_conflicting),
        "taxonomy_adjudication_id": taxonomy_adjudication_id,
        "taxonomy_expansion_status_override": taxonomy_expansion_status_override,
    })
    pre_event_feature_snapshot_hash = (
        canonical_hash(pre_event_metrics) if pre_event_metrics is not None else ""
    )
    learned_direction_snapshot_hash = (
        canonical_hash({
            "snapshot_id": learned_direction_snapshot_id,
            "proxy_direction": learned_proxy_direction,
            "confidence": float(learned_direction_confidence),
            "direction_calibration_snapshot_id": direction_calibration_snapshot_id,
            "event_empirical_precision": event_direction_empirical_precision,
            "event_calibrated_confidence": event_direction_calibrated_confidence,
        })
        if learned_proxy_direction or event_direction_calibrated_confidence is not None
        else ""
    )
    external_gate_state_hash = canonical_hash({
        "market_data_quality_pass": bool(market_data_quality_pass),
        "liquidity_pass": bool(liquidity_pass),
        "policy_gate_pass": bool(policy_gate_pass),
    })
    threshold_config_hash = canonical_hash(asdict(thresholds))
    policy_config_hash = canonical_hash(asdict(policy_config))
    return V5SemanticMapping(
        text,
        provider,
        causal_eligible,
        causal_exclusion_reason,
        recovered_event_candidate,
        recovered_event_clause,
        framing_clause,
        core_event_observation_eligible,
        news_content_type,
        content_type_status,
        content_type_strict_eligible,
        content_type_adjudication_id,
        event_type,
        referenced_event_types,
        region_scope,
        region_confidence,
        regions,
        channel_confidence,
        channels,
        event_direction,
        event_direction_confidence,
        event_direction_rule_strength,
        event_direction_empirical_precision,
        direction_calibration_snapshot_id,
        directions,
        family,
        family_confidence,
        family_candidates,
        family_abstained,
        family_reason,
        family_watch_only,
        selected_proxy,
        0.0 if proxy_abstained else proxy_confidence,
        proxy_gap,
        proxy_candidates,
        proxy_abstained,
        proxy_reason,
        critical_conflicts,
        semantic_mapping_strict,
        directional_strict,
        reaction_observation_eligible,
        trade_candidate_eligible,
        directional_trade_candidate_eligible,
        tier,
        semantic_input_hash,
        pre_event_feature_snapshot_id,
        pre_event_feature_snapshot_hash,
        learned_direction_snapshot_id,
        learned_direction_snapshot_hash,
        external_gate_state_hash,
        threshold_config_hash,
        policy_config_hash,
        recovery_action_status=recovery_action_status,
        decision_subject_type=decision_detection.subject_type,
        decision_temporality=decision_detection.temporality,
        taxonomy_expansion_requires_adjudication=taxonomy_expansion_requires_adjudication,
        taxonomy_expansion_status=taxonomy_expansion_status,
        taxonomy_adjudication_id=taxonomy_adjudication_id,
        taxonomy_strict_eligible=taxonomy_strict_eligible,
        sample_purpose=sample_purpose,
        strict_training_eligible=strict_training_eligible,
    )


@dataclass(frozen=True)
class V5PersistenceKeys:
    semantic_run_key: str
    proxy_ranking_run_key: str
    policy_evaluation_run_key: str


def _ensure_schema_column(
    conn: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def ensure_s05p4_v5_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS s05p4_v5_semantic_runs(
            semantic_run_key TEXT PRIMARY KEY,
            event_cluster_id INTEGER NOT NULL,
            snapshot_id TEXT NOT NULL,
            semantic_version TEXT NOT NULL,
            semantic_input_hash TEXT NOT NULL,
            threshold_config_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            causal_eligible INTEGER NOT NULL,
            causal_exclusion_reason TEXT NOT NULL,
            recovered_event_candidate INTEGER NOT NULL,
            recovered_event_clause TEXT NOT NULL,
            framing_clause TEXT NOT NULL,
            core_event_observation_eligible INTEGER NOT NULL,
            news_content_type TEXT NOT NULL,
            content_type_status TEXT NOT NULL,
            content_type_strict_eligible INTEGER NOT NULL,
            content_type_adjudication_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            referenced_event_types_json TEXT NOT NULL,
            region_scope TEXT NOT NULL,
            region_confidence REAL NOT NULL,
            channel_confidence REAL NOT NULL,
            event_direction TEXT NOT NULL,
            event_direction_rule_strength REAL NOT NULL,
            proxy_family TEXT NOT NULL,
            proxy_family_confidence REAL NOT NULL,
            proxy_family_abstained INTEGER NOT NULL,
            proxy_family_abstain_reason TEXT NOT NULL,
            semantic_mapping_strict INTEGER NOT NULL,
            critical_conflicts_json TEXT NOT NULL,
            recovery_action_status TEXT NOT NULL DEFAULT 'not_applicable',
            decision_subject_type TEXT NOT NULL DEFAULT 'not_applicable',
            decision_temporality TEXT NOT NULL DEFAULT 'not_applicable',
            taxonomy_expansion_requires_adjudication INTEGER NOT NULL DEFAULT 0,
            taxonomy_expansion_status TEXT NOT NULL DEFAULT 'not_applicable',
            taxonomy_adjudication_id TEXT NOT NULL DEFAULT '',
            taxonomy_strict_eligible INTEGER NOT NULL DEFAULT 1,
            sample_purpose TEXT NOT NULL DEFAULT 'standard_semantic_research',
            strict_training_eligible INTEGER NOT NULL DEFAULT 0,
            build_id TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            UNIQUE(event_cluster_id,snapshot_id,semantic_version,semantic_input_hash,threshold_config_hash)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_event_regions(
            semantic_run_key TEXT NOT NULL,
            region_code TEXT NOT NULL,
            region_role TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_text TEXT NOT NULL,
            evidence_start INTEGER NOT NULL,
            evidence_end INTEGER NOT NULL,
            relation_type TEXT NOT NULL,
            action_status TEXT NOT NULL,
            resolver_version TEXT NOT NULL,
            PRIMARY KEY(semantic_run_key,region_code,region_role),
            FOREIGN KEY(semantic_run_key) REFERENCES s05p4_v5_semantic_runs(semantic_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_event_channels(
            semantic_run_key TEXT NOT NULL,
            channel_code TEXT NOT NULL,
            channel_role TEXT NOT NULL,
            mechanism_direction TEXT NOT NULL,
            confidence REAL NOT NULL,
            evidence_text TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            classifier_version TEXT NOT NULL,
            PRIMARY KEY(semantic_run_key,channel_code,channel_role),
            FOREIGN KEY(semantic_run_key) REFERENCES s05p4_v5_semantic_runs(semantic_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_event_directions(
            semantic_run_key TEXT NOT NULL,
            direction_type TEXT NOT NULL,
            subject TEXT NOT NULL,
            direction TEXT NOT NULL,
            rule_strength REAL NOT NULL,
            empirical_precision REAL,
            calibrated_confidence REAL,
            evidence_text TEXT NOT NULL,
            PRIMARY KEY(semantic_run_key,direction_type,subject),
            FOREIGN KEY(semantic_run_key) REFERENCES s05p4_v5_semantic_runs(semantic_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_family_candidates(
            semantic_run_key TEXT NOT NULL,
            proxy_family TEXT NOT NULL,
            confidence REAL NOT NULL,
            required_evidence_pass INTEGER NOT NULL,
            evidence_text TEXT NOT NULL,
            validator_rule_id TEXT NOT NULL,
            validator_version TEXT NOT NULL,
            PRIMARY KEY(semantic_run_key,proxy_family),
            FOREIGN KEY(semantic_run_key) REFERENCES s05p4_v5_semantic_runs(semantic_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_proxy_ranking_runs(
            proxy_ranking_run_key TEXT PRIMARY KEY,
            semantic_run_key TEXT NOT NULL,
            ranker_version TEXT NOT NULL,
            pre_event_feature_snapshot_id TEXT NOT NULL,
            pre_event_feature_snapshot_hash TEXT NOT NULL,
            threshold_config_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            primary_proxy_selected TEXT NOT NULL,
            primary_proxy_confidence REAL NOT NULL,
            top1_top2_score_gap REAL NOT NULL,
            proxy_abstained INTEGER NOT NULL,
            proxy_abstain_reason TEXT NOT NULL,
            build_id TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(semantic_run_key) REFERENCES s05p4_v5_semantic_runs(semantic_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_proxy_candidates(
            proxy_ranking_run_key TEXT NOT NULL,
            proxy_family TEXT NOT NULL,
            symbol TEXT NOT NULL,
            proxy_role TEXT NOT NULL,
            expression_type TEXT NOT NULL,
            semantic_relevance REAL NOT NULL,
            region_specificity REAL NOT NULL,
            channel_specificity REAL NOT NULL,
            tradability_score REAL NOT NULL,
            spread_quality REAL NOT NULL,
            intraday_trade_coverage REAL NOT NULL,
            stale_trade_quality REAL NOT NULL,
            market_session_overlap REAL NOT NULL,
            family_exposure REAL NOT NULL,
            proxy_purity REAL NOT NULL,
            data_coverage_score REAL NOT NULL,
            final_score REAL NOT NULL,
            rank INTEGER NOT NULL,
            expected_direction TEXT NOT NULL,
            expected_direction_confidence REAL NOT NULL,
            ranker_version TEXT NOT NULL,
            PRIMARY KEY(proxy_ranking_run_key,symbol,proxy_role),
            FOREIGN KEY(proxy_ranking_run_key) REFERENCES s05p4_v5_proxy_ranking_runs(proxy_ranking_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_policy_evaluation_runs(
            policy_evaluation_run_key TEXT PRIMARY KEY,
            semantic_run_key TEXT NOT NULL,
            proxy_ranking_run_key TEXT NOT NULL,
            policy_config_hash TEXT NOT NULL,
            learned_direction_snapshot_id TEXT NOT NULL,
            learned_direction_snapshot_hash TEXT NOT NULL,
            direction_calibration_snapshot_id TEXT NOT NULL,
            external_gate_state_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            event_direction_calibrated_confidence REAL NOT NULL,
            event_direction_empirical_precision REAL,
            family_watch_only INTEGER NOT NULL,
            reaction_observation_eligible INTEGER NOT NULL,
            directional_strict INTEGER NOT NULL,
            trade_candidate_eligible INTEGER NOT NULL,
            directional_trade_candidate_eligible INTEGER NOT NULL,
            mapping_tier TEXT NOT NULL,
            build_id TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            FOREIGN KEY(semantic_run_key) REFERENCES s05p4_v5_semantic_runs(semantic_run_key),
            FOREIGN KEY(proxy_ranking_run_key) REFERENCES s05p4_v5_proxy_ranking_runs(proxy_ranking_run_key)
        );
        CREATE TABLE IF NOT EXISTS s05p4_v5_semantic_adjudications(
            review_row_id TEXT NOT NULL,
            field_name TEXT NOT NULL,
            proposed_value TEXT NOT NULL,
            reviewer_a_value TEXT NOT NULL,
            reviewer_b_value TEXT NOT NULL,
            final_adjudicated_value TEXT NOT NULL,
            disagreement_type TEXT NOT NULL,
            adjudication_reason TEXT NOT NULL,
            rule_candidate TEXT NOT NULL,
            adjudication_version TEXT NOT NULL,
            created_at_utc TEXT NOT NULL,
            PRIMARY KEY(review_row_id,field_name,adjudication_version)
        );
        """
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "content_type_status",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "content_type_strict_eligible",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "content_type_adjudication_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "referenced_event_types_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "recovery_action_status",
        "TEXT NOT NULL DEFAULT 'not_applicable'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "decision_subject_type",
        "TEXT NOT NULL DEFAULT 'not_applicable'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "decision_temporality",
        "TEXT NOT NULL DEFAULT 'not_applicable'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "taxonomy_expansion_requires_adjudication",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "taxonomy_expansion_status",
        "TEXT NOT NULL DEFAULT 'not_applicable'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "taxonomy_adjudication_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "taxonomy_strict_eligible",
        "INTEGER NOT NULL DEFAULT 1",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "sample_purpose",
        "TEXT NOT NULL DEFAULT 'standard_semantic_research'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_semantic_runs", "strict_training_eligible",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_event_regions", "relation_type",
        "TEXT NOT NULL DEFAULT 'contextual'",
    )
    _ensure_schema_column(
        conn, "s05p4_v5_event_regions", "action_status",
        "TEXT NOT NULL DEFAULT 'unclear'",
    )


def _assert_immutable_content(
    conn: sqlite3.Connection,
    table: str,
    key_column: str,
    key: str,
    expected_content_hash: str,
) -> None:
    actual = conn.execute(
        f"SELECT content_hash FROM {table} WHERE {key_column}=?", (key,)
    ).fetchone()
    if actual is None or actual[0] != expected_content_hash:
        raise sqlite3.IntegrityError(f"immutable content collision in {table}: {key}")


def persist_v5_mapping(
    conn: sqlite3.Connection,
    *,
    event_cluster_id: int,
    snapshot_id: str,
    mapping: V5SemanticMapping,
    build_id: str = "",
    config_hash: str = "",
) -> V5PersistenceKeys:
    ensure_s05p4_v5_schema(conn)
    effective_policy_hash = config_hash or mapping.policy_config_hash
    semantic_payload = {
        "causal_eligible": mapping.causal_eligible,
        "causal_exclusion_reason": mapping.causal_exclusion_reason,
        "recovered_event_candidate": mapping.recovered_event_candidate,
        "recovered_event_clause": mapping.recovered_event_clause,
        "framing_clause": mapping.framing_clause,
        "recovery_action_status": mapping.recovery_action_status,
        "core_event_observation_eligible": mapping.core_event_observation_eligible,
        "news_content_type": mapping.news_content_type,
        "content_type_status": mapping.content_type_status,
        "content_type_strict_eligible": mapping.content_type_strict_eligible,
        "content_type_adjudication_id": mapping.content_type_adjudication_id,
        "event_type": mapping.event_type,
        "referenced_event_types": mapping.referenced_event_types,
        "decision_subject_type": mapping.decision_subject_type,
        "decision_temporality": mapping.decision_temporality,
        "taxonomy_expansion_requires_adjudication": mapping.taxonomy_expansion_requires_adjudication,
        "taxonomy_expansion_status": mapping.taxonomy_expansion_status,
        "taxonomy_adjudication_id": mapping.taxonomy_adjudication_id,
        "taxonomy_strict_eligible": mapping.taxonomy_strict_eligible,
        "sample_purpose": mapping.sample_purpose,
        "strict_training_eligible": mapping.strict_training_eligible,
        "region_scope": mapping.region_scope,
        "region_confidence": mapping.region_confidence,
        "regions": [asdict(row) for row in mapping.regions],
        "channel_confidence": mapping.channel_confidence,
        "channels": [asdict(row) for row in mapping.channels],
        "event_direction": mapping.event_direction,
        "event_direction_rule_strength": mapping.event_direction_rule_strength,
        "semantic_directions": [
            asdict(row) for row in mapping.directions if row.direction_type != "proxy"
        ],
        "family_candidates": [asdict(row) for row in mapping.family_candidates],
        "proxy_family": mapping.proxy_family,
        "proxy_family_confidence": mapping.proxy_family_confidence,
        "proxy_family_abstained": mapping.proxy_family_abstained,
        "proxy_family_abstain_reason": mapping.proxy_family_abstain_reason,
        "semantic_mapping_strict": mapping.semantic_mapping_strict,
        "critical_conflicts": mapping.critical_conflicts,
    }
    semantic_content_hash = canonical_hash(semantic_payload)
    semantic_run_key = canonical_hash({
        "event_cluster_id": int(event_cluster_id),
        "snapshot_id": snapshot_id,
        "semantic_version": mapping.semantic_version,
        "semantic_input_hash": mapping.semantic_input_hash,
        "threshold_config_hash": mapping.threshold_config_hash,
    })
    ranking_payload = {
        "pre_event_feature_snapshot_id": mapping.pre_event_feature_snapshot_id,
        "pre_event_feature_snapshot_hash": mapping.pre_event_feature_snapshot_hash,
        "proxy_candidates": [asdict(row) for row in mapping.proxy_candidates],
        "primary_proxy_selected": mapping.primary_proxy_selected,
        "primary_proxy_confidence": mapping.primary_proxy_confidence,
        "top1_top2_score_gap": mapping.top1_top2_score_gap,
        "proxy_abstained": mapping.proxy_abstained,
        "proxy_abstain_reason": mapping.proxy_abstain_reason,
    }
    ranking_content_hash = canonical_hash(ranking_payload)
    proxy_ranking_run_key = canonical_hash({
        "semantic_run_key": semantic_run_key,
        "ranker_version": V5_PROXY_RANKER_VERSION,
        "pre_event_feature_snapshot_id": mapping.pre_event_feature_snapshot_id,
        "pre_event_feature_snapshot_hash": mapping.pre_event_feature_snapshot_hash,
        "threshold_config_hash": mapping.threshold_config_hash,
    })
    policy_payload = {
        "learned_direction_snapshot_id": mapping.learned_direction_snapshot_id,
        "learned_direction_snapshot_hash": mapping.learned_direction_snapshot_hash,
        "direction_calibration_snapshot_id": mapping.direction_calibration_snapshot_id,
        "external_gate_state_hash": mapping.external_gate_state_hash,
        "event_direction_confidence": mapping.event_direction_confidence,
        "event_direction_empirical_precision": mapping.event_direction_empirical_precision,
        "family_watch_only": mapping.family_watch_only,
        "reaction_observation_eligible": mapping.reaction_observation_eligible,
        "directional_strict": mapping.directional_strict,
        "trade_candidate_eligible": mapping.trade_candidate_eligible,
        "directional_trade_candidate_eligible": mapping.directional_trade_candidate_eligible,
        "mapping_tier": mapping.mapping_tier,
    }
    policy_content_hash = canonical_hash(policy_payload)
    policy_evaluation_run_key = canonical_hash({
        "semantic_run_key": semantic_run_key,
        "proxy_ranking_run_key": proxy_ranking_run_key,
        "policy_config_hash": effective_policy_hash,
        "learned_direction_snapshot_hash": mapping.learned_direction_snapshot_hash,
        "external_gate_state_hash": mapping.external_gate_state_hash,
    })
    now = datetime.now(UTC).isoformat()

    with conn:
        conn.execute(
            """INSERT OR IGNORE INTO s05p4_v5_semantic_runs(
                semantic_run_key,event_cluster_id,snapshot_id,semantic_version,
                semantic_input_hash,threshold_config_hash,content_hash,
                causal_eligible,causal_exclusion_reason,recovered_event_candidate,
                recovered_event_clause,framing_clause,core_event_observation_eligible,
                news_content_type,content_type_status,content_type_strict_eligible,
                content_type_adjudication_id,event_type,referenced_event_types_json,
                region_scope,region_confidence,channel_confidence,event_direction,
                event_direction_rule_strength,proxy_family,proxy_family_confidence,
                proxy_family_abstained,proxy_family_abstain_reason,
                semantic_mapping_strict,critical_conflicts_json,build_id,created_at_utc
            ) VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
            (
                semantic_run_key, int(event_cluster_id), snapshot_id, mapping.semantic_version,
                mapping.semantic_input_hash, mapping.threshold_config_hash, semantic_content_hash,
                int(mapping.causal_eligible), mapping.causal_exclusion_reason,
                int(mapping.recovered_event_candidate), mapping.recovered_event_clause,
                mapping.framing_clause, int(mapping.core_event_observation_eligible),
                mapping.news_content_type, mapping.content_type_status,
                int(mapping.content_type_strict_eligible), mapping.content_type_adjudication_id,
                mapping.event_type, json.dumps(mapping.referenced_event_types, sort_keys=True),
                mapping.region_scope,
                mapping.region_confidence, mapping.channel_confidence, mapping.event_direction,
                mapping.event_direction_rule_strength, mapping.proxy_family,
                mapping.proxy_family_confidence, int(mapping.proxy_family_abstained),
                mapping.proxy_family_abstain_reason, int(mapping.semantic_mapping_strict),
                json.dumps(mapping.critical_conflicts, sort_keys=True), build_id, now,
            ),
        )
        _assert_immutable_content(
            conn, "s05p4_v5_semantic_runs", "semantic_run_key",
            semantic_run_key, semantic_content_hash
        )
        conn.execute(
            """UPDATE s05p4_v5_semantic_runs
                  SET recovery_action_status=?, decision_subject_type=?,
                      decision_temporality=?, taxonomy_expansion_requires_adjudication=?,
                      taxonomy_expansion_status=?, taxonomy_adjudication_id=?,
                      taxonomy_strict_eligible=?, sample_purpose=?, strict_training_eligible=?
                WHERE semantic_run_key=?""",
            (
                mapping.recovery_action_status,
                mapping.decision_subject_type,
                mapping.decision_temporality,
                int(mapping.taxonomy_expansion_requires_adjudication),
                mapping.taxonomy_expansion_status,
                mapping.taxonomy_adjudication_id,
                int(mapping.taxonomy_strict_eligible),
                mapping.sample_purpose,
                int(mapping.strict_training_eligible),
                semantic_run_key,
            ),
        )
        conn.executemany(
            """INSERT OR IGNORE INTO s05p4_v5_event_regions(
                semantic_run_key,region_code,region_role,confidence,evidence_text,
                evidence_start,evidence_end,relation_type,action_status,resolver_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            [(
                semantic_run_key, row.region_code, row.region_role, row.confidence,
                row.evidence_text, row.evidence_start, row.evidence_end,
                row.relation_type, row.action_status, row.resolver_version
            ) for row in mapping.regions],
        )
        conn.executemany(
            """INSERT OR IGNORE INTO s05p4_v5_event_channels VALUES(?,?,?,?,?,?,?,?)""",
            [(
                semantic_run_key, row.channel_code, row.channel_role,
                row.mechanism_direction, row.confidence, row.evidence_text,
                row.rule_id, row.classifier_version
            ) for row in mapping.channels],
        )
        conn.executemany(
            """INSERT OR IGNORE INTO s05p4_v5_event_directions VALUES(?,?,?,?,?,?,?,?)""",
            [(
                semantic_run_key, row.direction_type, row.subject, row.direction,
                row.rule_strength, row.empirical_precision, row.calibrated_confidence,
                row.evidence_text
            ) for row in mapping.directions if row.direction_type != "proxy"],
        )
        conn.executemany(
            """INSERT OR IGNORE INTO s05p4_v5_family_candidates VALUES(?,?,?,?,?,?,?)""",
            [(
                semantic_run_key, row.proxy_family, row.confidence,
                int(row.required_evidence_pass), row.evidence_text,
                row.validator_rule_id, row.validator_version
            ) for row in mapping.family_candidates],
        )
        conn.execute(
            """INSERT OR IGNORE INTO s05p4_v5_proxy_ranking_runs VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
            (
                proxy_ranking_run_key, semantic_run_key, V5_PROXY_RANKER_VERSION,
                mapping.pre_event_feature_snapshot_id, mapping.pre_event_feature_snapshot_hash,
                mapping.threshold_config_hash, ranking_content_hash,
                mapping.primary_proxy_selected, mapping.primary_proxy_confidence,
                mapping.top1_top2_score_gap, int(mapping.proxy_abstained),
                mapping.proxy_abstain_reason, build_id, now,
            ),
        )
        _assert_immutable_content(
            conn, "s05p4_v5_proxy_ranking_runs", "proxy_ranking_run_key",
            proxy_ranking_run_key, ranking_content_hash
        )
        conn.executemany(
            """INSERT OR IGNORE INTO s05p4_v5_proxy_candidates VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
            [(
                proxy_ranking_run_key, row.proxy_family, row.symbol, row.proxy_role,
                row.expression_type, row.semantic_relevance, row.region_specificity,
                row.channel_specificity, row.tradability_score, row.spread_quality,
                row.intraday_trade_coverage, row.stale_trade_quality,
                row.market_session_overlap, row.family_exposure, row.proxy_purity,
                row.data_coverage_score, row.final_score, row.rank,
                row.expected_direction, row.expected_direction_confidence, row.ranker_version,
            ) for row in mapping.proxy_candidates],
        )
        conn.execute(
            """INSERT OR IGNORE INTO s05p4_v5_policy_evaluation_runs VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
            (
                policy_evaluation_run_key, semantic_run_key, proxy_ranking_run_key,
                effective_policy_hash, mapping.learned_direction_snapshot_id,
                mapping.learned_direction_snapshot_hash,
                mapping.direction_calibration_snapshot_id, mapping.external_gate_state_hash,
                policy_content_hash, mapping.event_direction_confidence,
                mapping.event_direction_empirical_precision, int(mapping.family_watch_only),
                int(mapping.reaction_observation_eligible), int(mapping.directional_strict),
                int(mapping.trade_candidate_eligible),
                int(mapping.directional_trade_candidate_eligible), mapping.mapping_tier,
                build_id, now,
            ),
        )
        _assert_immutable_content(
            conn, "s05p4_v5_policy_evaluation_runs", "policy_evaluation_run_key",
            policy_evaluation_run_key, policy_content_hash
        )
    return V5PersistenceKeys(
        semantic_run_key,
        proxy_ranking_run_key,
        policy_evaluation_run_key,
    )
