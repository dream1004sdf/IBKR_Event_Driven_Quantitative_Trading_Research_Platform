from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from recorder.market_outcomes import IntradayBar
from recorder.m236_news_learning import (
    M236NewsLearningConfig,
    M236OutcomeAttachmentConfig,
    attach_m236_news_outcomes,
    build_m236_news_learning_report,
    render_m236_news_learning_markdown,
)


def _append_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


class _FakeIntradayPriceSource:
    name = "fake_intraday"

    def __init__(self, bars_by_symbol: dict[str, list[IntradayBar]]) -> None:
        self.bars_by_symbol = bars_by_symbol
        self.requests: list[tuple[str, object, object]] = []

    def get_intraday_bars(self, symbol: str, start, end) -> list[IntradayBar]:
        self.requests.append((symbol, start, end))
        return self.bars_by_symbol.get(symbol.upper(), [])


def test_m236_news_learning_builds_provider_ticker_and_scorer_directives(tmp_path: Path) -> None:
    event_roles = tmp_path / "m236.json"
    event_roles.write_text(json.dumps({"version": "m236-cross-provider-event-role-test"}), encoding="utf-8")
    jsonl = tmp_path / "learning.jsonl"
    _append_jsonl(
        jsonl,
        [
            {
                "learning_record_key": "confirmed-1",
                "canonical_event_id": "event-1",
                "canonical_event_key": "NVDA|sector|ai chip demand",
                "event_time": "2026-07-02T10:00:00+00:00",
                "target_assets": ["NVDA"],
                "provider_agreement": "confirmed",
                "confirmation_quality": "medium",
                "role_family": "sector",
                "direction_counts": {"positive": 2},
                "confidence_adjustment": 0.08,
                "adaptive_confidence_cap": {"positive_max": 0.12, "negative_min": 0.0},
                "provider_families": ["benzinga", "dow_jones"],
                "ticker_inference_confidence": 0.95,
                "horizons": ["5M", "15M"],
                "outcomes": {
                    "5M": {"raw_return": 0.004, "direction_label": "up"},
                    "15M": {"raw_return": 0.002, "direction_label": "up"},
                },
            },
            {
                "learning_record_key": "noise-1",
                "canonical_event_id": "event-2",
                "canonical_event_key": "MARKET|context|noise",
                "event_time": "2026-07-02T10:00:00+00:00",
                "target_assets": [],
                "provider_agreement": "marketaux_noise_filtered",
                "confirmation_quality": "filtered",
                "role_family": "context",
                "direction_counts": {"neutral": 1},
                "confidence_adjustment": 0.0,
                "adaptive_confidence_cap": {"positive_max": 0.0, "negative_min": 0.0},
                "provider_families": ["marketaux"],
                "ticker_inference_confidence": 0.10,
                "horizons": ["5M"],
            },
        ],
    )

    payload = build_m236_news_learning_report(
        M236NewsLearningConfig(
            event_roles_json_path=event_roles,
            outcome_learning_jsonl_path=jsonl,
            provider_profile_output_path=tmp_path / "provider.json",
            report_json_output_path=tmp_path / "report.json",
            report_markdown_output_path=tmp_path / "report.md",
        )
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["record_count"] == 2
    assert payload["summary"]["status_counts"]["evaluated"] == 1
    assert payload["provider_profile"]["benzinga"]["hit_rate"] == 1.0
    assert payload["ticker_inference_verification"]["confidence_buckets"]["low"] == 1
    assert payload["scorer_impact"]["directive_counts"]["confirmed_source_quality"] == 1
    assert payload["scorer_impact"]["directive_counts"]["marketaux_noise_filtered_low_ticker_confidence"] == 1
    assert "M236 News Source-Quality Learning" in render_m236_news_learning_markdown(payload)


def test_m236_news_learning_excludes_alpha_vantage_records_from_active_training(tmp_path: Path) -> None:
    event_roles = tmp_path / "m236.json"
    event_roles.write_text(json.dumps({"version": "m236-cross-provider-event-role-test"}), encoding="utf-8")
    jsonl = tmp_path / "learning.jsonl"
    _append_jsonl(
        jsonl,
        [
            {
                "learning_record_key": "alpha-1",
                "canonical_event_id": "event-alpha",
                "canonical_event_key": "SPY|macro|alpha sentiment",
                "event_time": "2026-07-02T10:00:00+00:00",
                "target_assets": ["SPY"],
                "provider_agreement": "external_only_or_unconfirmed",
                "role_family": "macro",
                "direction_counts": {"positive": 1},
                "provider_families": ["alpha_vantage"],
                "ticker_inference_confidence": 0.65,
                "horizons": ["5M"],
            },
            {
                "learning_record_key": "marketaux-1",
                "canonical_event_id": "event-marketaux",
                "canonical_event_key": "SPY|macro|marketaux sentiment",
                "event_time": "2026-07-02T10:00:00+00:00",
                "target_assets": ["SPY"],
                "provider_agreement": "external_only_or_unconfirmed",
                "role_family": "macro",
                "direction_counts": {"positive": 1},
                "provider_families": ["marketaux"],
                "ticker_inference_confidence": 0.65,
                "horizons": ["5M"],
            },
        ],
    )

    payload = build_m236_news_learning_report(
        M236NewsLearningConfig(
            event_roles_json_path=event_roles,
            outcome_learning_jsonl_path=jsonl,
            provider_profile_output_path=tmp_path / "provider.json",
            report_json_output_path=tmp_path / "report.json",
            report_markdown_output_path=tmp_path / "report.md",
        )
    )

    assert payload["m236_news_learning_version"] == "m236-news-source-quality-learning-v3-no-alpha-active-training"
    assert payload["summary"]["record_count"] == 1
    assert payload["retention"]["alpha_vantage_excluded_record_count"] == 1
    assert "alpha_vantage" not in payload["provider_profile"]


def test_m236_outcome_attachment_adds_due_intraday_outcomes(tmp_path: Path) -> None:
    event_time = "2026-07-01T14:30:00+00:00"
    jsonl = tmp_path / "learning.jsonl"
    _append_jsonl(
        jsonl,
        [
            {
                "learning_record_key": "pending-1",
                "canonical_event_id": "event-1",
                "canonical_event_key": "NVDA|earnings|guidance",
                "event_time": event_time,
                "target_assets": ["NVDA"],
                "provider_agreement": "confirmed",
                "confirmation_quality": "high",
                "role_family": "earnings",
                "direction_counts": {"positive": 2},
                "provider_families": ["benzinga", "dow_jones"],
                "ticker_inference_confidence": 0.98,
                "horizons": ["5M", "15M"],
            }
        ],
    )
    bars = [
        IntradayBar(symbol="NVDA", timestamp=datetime.fromisoformat("2026-07-01T14:29:00+00:00"), close=100.0),
        IntradayBar(symbol="NVDA", timestamp=datetime.fromisoformat("2026-07-01T14:35:00+00:00"), close=101.0),
        IntradayBar(symbol="NVDA", timestamp=datetime.fromisoformat("2026-07-01T14:45:00+00:00"), close=99.5),
    ]
    price_source = _FakeIntradayPriceSource({"NVDA": bars})

    payload = attach_m236_news_outcomes(
        M236OutcomeAttachmentConfig(
            outcome_learning_jsonl_path=jsonl,
            report_json_output_path=tmp_path / "report.json",
            report_markdown_output_path=tmp_path / "report.md",
            max_records=5,
            max_symbols=2,
            horizons=("5M", "15M"),
            lookback_records=100,
        ),
        price_source=price_source,
    )

    assert payload["status"] == "ok"
    assert payload["summary"]["attached_records"] == 1
    assert payload["summary"]["attached_horizons"] == 2
    updated = json.loads(jsonl.read_text(encoding="utf-8").splitlines()[0])
    assert updated["outcomes"]["5M"]["direction_label"] == "up"
    assert updated["outcomes"]["15M"]["direction_label"] == "down"
    assert updated["outcome_attachment"]["attached_horizon_count"] == 2
    assert price_source.requests and price_source.requests[0][0] == "NVDA"


def test_m236_outcome_attachment_dry_run_does_not_mutate(tmp_path: Path) -> None:
    jsonl = tmp_path / "learning.jsonl"
    row = {
        "learning_record_key": "pending-2",
        "event_time": "2026-07-01T14:30:00+00:00",
        "target_assets": ["SPX"],
        "direction_counts": {"negative": 1},
        "provider_families": ["dow_jones"],
        "horizons": ["5M"],
    }
    _append_jsonl(jsonl, [row])
    before = jsonl.read_text(encoding="utf-8")
    payload = attach_m236_news_outcomes(
        M236OutcomeAttachmentConfig(
            outcome_learning_jsonl_path=jsonl,
            max_records=5,
            max_symbols=2,
            horizons=("5M",),
            dry_run=True,
        ),
        price_source=_FakeIntradayPriceSource({}),
    )

    assert payload["summary"]["action_status_counts"]["dry_run_due"] == 1
    assert jsonl.read_text(encoding="utf-8") == before


def test_m236_outcome_attachment_uses_broadtape_and_index_proxy_fallbacks(tmp_path: Path) -> None:
    jsonl = tmp_path / "learning.jsonl"
    _append_jsonl(
        jsonl,
        [
            {
                "learning_record_key": "pending-3",
                "event_time": "2026-07-01T14:30:00+00:00",
                "target_assets": ["BROADTAPE:BZ"],
                "canonical_event_key": "BROADTAPE:BZ|market_color|stocks move higher",
                "role_family": "market_color",
                "direction_counts": {"positive": 1},
                "provider_families": ["emergency_breaking_news"],
                "horizons": ["5M"],
            },
            {
                "learning_record_key": "pending-4",
                "event_time": "2026-07-01T14:30:00+00:00",
                "target_assets": ["NDX"],
                "canonical_event_key": "NDX|market_color|nasdaq rallies",
                "role_family": "market_color",
                "direction_counts": {"positive": 1},
                "provider_families": ["dow_jones"],
                "horizons": ["5M"],
            },
        ],
    )
    bars_by_symbol = {
        "SPY": [
            IntradayBar(symbol="SPY", timestamp=datetime.fromisoformat("2026-07-01T14:29:00+00:00"), close=100.0),
            IntradayBar(symbol="SPY", timestamp=datetime.fromisoformat("2026-07-01T14:35:00+00:00"), close=100.5),
        ],
        "QQQ": [
            IntradayBar(symbol="QQQ", timestamp=datetime.fromisoformat("2026-07-01T14:29:00+00:00"), close=200.0),
            IntradayBar(symbol="QQQ", timestamp=datetime.fromisoformat("2026-07-01T14:35:00+00:00"), close=201.0),
        ],
    }
    price_source = _FakeIntradayPriceSource(bars_by_symbol)

    payload = attach_m236_news_outcomes(
        M236OutcomeAttachmentConfig(
            outcome_learning_jsonl_path=jsonl,
            max_records=5,
            max_symbols=5,
            horizons=("5M",),
        ),
        price_source=price_source,
    )

    assert payload["summary"]["attached_records"] == 2
    updated = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert updated[0]["outcomes"]["5M"]["symbol"] == "SPY"
    assert updated[1]["outcomes"]["5M"]["symbol"] == "QQQ"


def test_m236_news_learning_retains_evaluated_rows_after_tail_window(tmp_path: Path) -> None:
    jsonl = tmp_path / "learning.jsonl"
    evaluated_index = tmp_path / "evaluated_index.jsonl"
    _append_jsonl(
        evaluated_index,
        [
            {
                "learning_record_key": "old-evaluated",
                "event_time": "2026-07-01T14:30:00+00:00",
                "target_assets": ["NVDA"],
                "provider_agreement": "confirmed",
                "role_family": "sector",
                "direction_counts": {"positive": 1},
                "provider_families": ["benzinga"],
                "ticker_inference_confidence": 0.95,
                "horizons": ["5M"],
                "outcomes": {"5M": {"raw_return": 0.002, "direction_label": "up"}},
            }
        ],
    )
    _append_jsonl(
        jsonl,
        [
            {
                "learning_record_key": "new-pending",
                "event_time": "2026-07-02T14:30:00+00:00",
                "target_assets": ["QQQ"],
                "provider_agreement": "external_only_or_unconfirmed",
                "role_family": "market_color",
                "direction_counts": {"neutral": 1},
                "provider_families": ["marketaux"],
                "ticker_inference_confidence": 0.8,
                "horizons": ["5M"],
            }
        ],
    )

    payload = build_m236_news_learning_report(
        M236NewsLearningConfig(
            outcome_learning_jsonl_path=jsonl,
            evaluated_index_jsonl_path=evaluated_index,
            report_json_output_path=tmp_path / "report.json",
            report_markdown_output_path=tmp_path / "report.md",
            provider_profile_output_path=tmp_path / "provider.json",
        )
    )

    assert payload["summary"]["record_count"] == 2
    assert payload["summary"]["evaluated_index_record_count"] == 1
    assert payload["summary"]["evaluated_retained_record_count"] == 1
    assert payload["provider_profile"]["benzinga"]["evaluated_horizon_count"] == 1


def test_m236_outcome_attachment_adaptive_batch_expands_when_backlog_is_high(tmp_path: Path) -> None:
    jsonl = tmp_path / "learning.jsonl"
    event_time = "2026-07-01T14:30:00+00:00"
    rows = [
        {
            "learning_record_key": f"pending-{idx}",
            "event_time": event_time,
            "target_assets": ["SPY"],
            "direction_counts": {"positive": 1},
            "provider_families": ["benzinga"],
            "ticker_inference_confidence": 0.9,
            "horizons": ["5M"],
        }
        for idx in range(6)
    ]
    _append_jsonl(jsonl, rows)
    bars = [
        IntradayBar(symbol="SPY", timestamp=datetime.fromisoformat("2026-07-01T14:29:00+00:00"), close=100.0),
        IntradayBar(symbol="SPY", timestamp=datetime.fromisoformat("2026-07-01T14:35:00+00:00"), close=101.0),
    ]

    payload = attach_m236_news_outcomes(
        M236OutcomeAttachmentConfig(
            outcome_learning_jsonl_path=jsonl,
            max_records=1,
            max_symbols=1,
            adaptive_backlog_mid=2,
            adaptive_backlog_high=5,
            adaptive_max_records_mid=3,
            adaptive_max_records_high=4,
            adaptive_max_symbols_high=2,
            horizons=("5M",),
        ),
        price_source=_FakeIntradayPriceSource({"SPY": bars}),
    )

    assert payload["adaptive_batch"]["tier"] == "high_backlog"
    assert payload["config"]["effective_max_records"] == 4
    assert payload["summary"]["processed_records"] == 4
    assert payload["summary"]["attached_records"] == 4


def test_m236_broad_proxy_guard_blocks_low_confidence_context_proxy(tmp_path: Path) -> None:
    jsonl = tmp_path / "learning.jsonl"
    _append_jsonl(
        jsonl,
        [
            {
                "learning_record_key": "guarded-context",
                "event_time": "2026-07-01T14:30:00+00:00",
                "target_assets": ["SPY"],
                "target_asset_mapping": {"mapped_assets": ["SPY"], "role_family_proxy_assets": ["SPY"], "direct_assets": []},
                "role_family": "context",
                "direction_counts": {"positive": 1},
                "provider_agreement": "weak_confirmation",
                "provider_families": ["marketaux"],
                "ticker_inference_confidence": 0.1,
                "horizons": ["5M"],
            }
        ],
    )
    bars = [
        IntradayBar(symbol="SPY", timestamp=datetime.fromisoformat("2026-07-01T14:29:00+00:00"), close=100.0),
        IntradayBar(symbol="SPY", timestamp=datetime.fromisoformat("2026-07-01T14:35:00+00:00"), close=101.0),
    ]

    attach_payload = attach_m236_news_outcomes(
        M236OutcomeAttachmentConfig(outcome_learning_jsonl_path=jsonl, horizons=("5M",), max_records=5),
        price_source=_FakeIntradayPriceSource({"SPY": bars}),
    )
    learning_payload = build_m236_news_learning_report(
        M236NewsLearningConfig(
            outcome_learning_jsonl_path=jsonl,
            evaluated_index_jsonl_path=tmp_path / "evaluated_index.jsonl",
            report_json_output_path=tmp_path / "report.json",
            report_markdown_output_path=tmp_path / "report.md",
            provider_profile_output_path=tmp_path / "provider.json",
        )
    )

    assert attach_payload["actions"][0]["reason"] == "no_reliable_target_asset"
    assert learning_payload["scorer_impact"]["top_directives"][0]["reason"].endswith("_broad_proxy_guard")
