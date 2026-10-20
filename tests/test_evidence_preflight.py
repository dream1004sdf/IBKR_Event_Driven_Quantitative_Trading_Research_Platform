from __future__ import annotations

import json
from pathlib import Path

from recorder.evidence_operations import (
    build_evidence_preflight_report,
    evidence_preflight_markdown,
    write_evidence_preflight_json,
)
from recorder.options_analysis import (
    build_autonomous_execution_gate_report,
    build_execution_intent_ledger_report,
)
from main import _build_watch_evidence_preflight_auto_gate


def _freshness_report(*, status: str = "watch") -> dict:
    return {
        "evidence_freshness_desk_version": "evidence-freshness-desk-v1",
        "overall_status": status,
        "status_counts": {"fresh": 5, "lagging": 1, "stale": 0, "offline": 0},
        "stale_source_keys": [],
        "lagging_source_keys": ["market_news"],
        "source_freshness_weights": {"market_news": 0.5, "economic_indicators": 1.0},
        "scorer_guard": {
            "action": "normal",
            "do_not_use_as_latest": [],
            "background_only": ["market_news"],
        },
        "sources": [
            {"source_key": "market_news", "status": "lagging", "scorer_weight": 0.5},
            {"source_key": "economic_indicators", "status": "fresh", "scorer_weight": 1.0},
        ],
    }


def _preview_payload() -> dict:
    return {
        "order_preview_version": "paper-order-preview-margin-whatif-v1",
        "preview_item_count": 1,
        "account_snapshot": {"effective_cash": 1000.0},
        "preview_items": [
            {
                "preview_status": "preview_ready",
                "symbol": "NVDA",
                "ticket_type": "stock",
                "ticket_draft": {
                    "ticket_type": "stock",
                    "order_action": "BUY",
                    "quantity": 10,
                    "estimated_notional": 100.0,
                },
                "contract_preview": {"sec_type": "STK", "symbol": "NVDA", "exchange": "SMART", "currency": "USD"},
                "ibkr_what_if_order_preview": {
                    "action": "BUY",
                    "source_order_action": "BUY",
                    "total_quantity": 10,
                    "what_if": True,
                    "transmit": False,
                },
                "local_margin_estimate": {
                    "estimated_initial_margin_change": 100.0,
                    "estimated_cash_impact": -100.0,
                },
            }
        ],
    }


def test_evidence_preflight_haircuts_lagging_sources(tmp_path: Path) -> None:
    watchdog = {
        "evidence_refresh_watchdog_version": "evidence-refresh-watchdog-v1",
        "status": "refresh_completed_gap_remains",
        "last_freshness_status": "watch",
        "consecutive_failures": 0,
        "last_lagging_source_keys": ["market_news"],
    }

    result = build_evidence_preflight_report(_freshness_report(), watchdog_payload=watchdog, lagging_size_multiplier=0.25)
    markdown = evidence_preflight_markdown(result)
    written = write_evidence_preflight_json(tmp_path / "preflight.json", result)
    round_trip = json.loads(written.read_text(encoding="utf-8"))

    assert result["evidence_preflight_version"] == "evidence-preflight-v1"
    assert result["gate_status"] == "watch"
    assert result["allowed"] is True
    assert result["action"] == "allow_with_evidence_haircut"
    assert result["entry_size_multiplier"] == 0.25
    assert result["freshness"]["lagging_source_keys"] == ["market_news"]
    assert "Evidence Preflight Gate" in markdown
    assert round_trip["action"] == "allow_with_evidence_haircut"


def test_evidence_preflight_blocks_stale_or_failed_recovery() -> None:
    stale = _freshness_report(status="yellow")
    stale["stale_source_keys"] = ["geopolitical_signals"]
    stale["status_counts"] = {"fresh": 4, "lagging": 1, "stale": 1, "offline": 0}
    stale["sources"].append({"source_key": "geopolitical_signals", "status": "stale", "scorer_weight": 0.0})
    watchdog = {"status": "refresh_failed", "consecutive_failures": 2}

    result = build_evidence_preflight_report(stale, watchdog_payload=watchdog)

    assert result["gate_status"] == "block"
    assert result["allowed"] is False
    assert result["action"] == "block_new_paper_entries"
    assert "evidence_stale_sources" in result["reason"]
    assert "evidence_auto_recovery_failed" in result["reason"]


def test_autonomous_gate_and_intent_ledger_apply_evidence_haircut() -> None:
    preflight = build_evidence_preflight_report(_freshness_report(), lagging_size_multiplier=0.5)

    auto_gate = build_autonomous_execution_gate_report(
        _preview_payload(),
        allow_fractional_shares=True,
        max_auto_orders=5,
        evidence_preflight_payload=preflight,
    )
    ledger = build_execution_intent_ledger_report(auto_gate, max_intents=5)

    assert auto_gate["auto_ready_count"] == 1
    item = auto_gate["auto_execution_items"][0]
    assert item["auto_execution_status"] == "auto_execution_ready"
    assert item["evidence_entry_size_multiplier"] == 0.5
    intent = ledger["intents"][0]
    assert intent["state"] == "pending_paper_execution"
    assert intent["evidence_preflight_gate_applied"] is True
    assert intent["quantity"] == 5
    assert intent["order_preview"]["total_quantity"] == 5
    assert intent["local_margin_estimate"]["estimated_initial_margin_change"] == 50.0


def test_autonomous_gate_blocks_when_evidence_preflight_blocks() -> None:
    preflight = build_evidence_preflight_report(
        {"overall_status": "yellow", "stale_source_keys": ["market_news"], "sources": [{"source_key": "market_news", "status": "stale"}]},
    )

    auto_gate = build_autonomous_execution_gate_report(
        _preview_payload(),
        allow_fractional_shares=True,
        max_auto_orders=5,
        evidence_preflight_payload=preflight,
    )

    assert auto_gate["auto_ready_count"] == 0
    assert auto_gate["auto_gate_status_counts"]["auto_execution_blocked"] == 1
    assert auto_gate["auto_gate_block_reason_counts"]["evidence_preflight_allows_new_paper_entries"] == 1


def test_watch_helper_builds_evidence_aware_auto_gate_from_existing_m113_json(tmp_path: Path) -> None:
    preview_path = tmp_path / "preview.json"
    freshness_path = tmp_path / "freshness.json"
    watchdog_path = tmp_path / "watchdog.json"
    source_auto_path = tmp_path / "source_auto_gate.json"
    preview_path.write_text(json.dumps(_preview_payload()), encoding="utf-8")
    freshness_path.write_text(json.dumps(_freshness_report()), encoding="utf-8")
    watchdog_path.write_text(
        json.dumps({"status": "refresh_completed_gap_remains", "consecutive_failures": 0, "last_lagging_source_keys": ["market_news"]}),
        encoding="utf-8",
    )
    source_auto_path.write_text(
        json.dumps(
            {
                "result": {
                    "autonomous_execution_gate_version": "paper-autonomous-execution-gate-v1",
                    "gate_parameters": {
                        "allow_fractional_shares": True,
                        "allow_short_stock": False,
                        "require_short_availability_for_shorts": True,
                        "require_broker_whatif": False,
                        "max_auto_orders": 5,
                        "min_cash_buffer": 0.0,
                        "allowed_ticket_types": ["stock"],
                        "allowed_actions": ["BUY", "SELL"],
                    },
                },
                "filters": {"preview_json": str(preview_path)},
            }
        ),
        encoding="utf-8",
    )
    profile = {
        "evidence_preflight_gate": {
            "enabled": True,
            "freshness_report_json": str(freshness_path),
            "watchdog_state_json": str(watchdog_path),
            "preflight_json_output_path": str(tmp_path / "m156_preflight.json"),
            "preflight_output": str(tmp_path / "m156_preflight.md"),
            "auto_gate_json_output_path": str(tmp_path / "m156_auto_gate.json"),
            "auto_gate_output": str(tmp_path / "m156_auto_gate.md"),
            "lagging_size_multiplier": 0.5,
        }
    }

    auto_gate_path, preflight, summary = _build_watch_evidence_preflight_auto_gate(
        profile=profile,
        profile_base=tmp_path,
        source_auto_gate_json_path=source_auto_path,
        run_id="test_m156",
    )
    payload = json.loads(auto_gate_path.read_text(encoding="utf-8"))["result"]

    assert summary["applied"] is True
    assert preflight["action"] == "allow_with_evidence_haircut"
    assert payload["gate_parameters"]["evidence_preflight_configured"] is True
    assert payload["gate_parameters"]["evidence_entry_size_multiplier"] == 0.5
    assert payload["auto_ready_count"] == 1
    assert payload["auto_execution_items"][0]["evidence_entry_size_multiplier"] == 0.5
    assert (tmp_path / "m156_preflight.md").exists()
    assert (tmp_path / "m156_auto_gate.md").exists()
