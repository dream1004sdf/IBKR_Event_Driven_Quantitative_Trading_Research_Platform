from __future__ import annotations

import json
import os
import time
from pathlib import Path

from recorder.execution_hot_path_latency_audit import (
    build_execution_hot_path_latency_audit,
    build_execution_hot_path_latency_audit_from_paths,
    execution_hot_path_latency_audit_markdown,
    write_execution_hot_path_latency_audit_json,
    write_execution_hot_path_latency_audit_markdown,
)


def _write(path: Path, payload: dict, *, age_seconds: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    ts = time.time() - age_seconds
    os.utime(path, (ts, ts))
    return path


def _profile(base: Path) -> dict:
    return {
        "hot_path_latency_budget": {
            "enabled": True,
            "max_dashboard_tick_seconds": 0.75,
            "max_release_governed_inline_seconds": 0.1,
            "max_total_artifact_read_ms": 100.0,
            "stages": {
                "source_evidence_packet": {
                    "json_path": str(base / "supervisor.json"),
                    "required": True,
                    "execution_mode": "prewarmed_artifact",
                    "max_artifact_age_seconds": 120.0,
                    "warn_artifact_age_seconds": 60.0,
                    "max_read_ms": 20.0,
                    "hard_gate": True,
                },
                "market_data_context": {
                    "json_path": str(base / "market.json"),
                    "required": True,
                    "execution_mode": "prewarmed_artifact",
                    "max_artifact_age_seconds": 120.0,
                    "warn_artifact_age_seconds": 60.0,
                    "max_read_ms": 20.0,
                    "hard_gate": True,
                },
                "session_transition_release_governor": {
                    "json_path": str(base / "release.json"),
                    "required": True,
                    "execution_mode": "cheap_inline_final_gate",
                    "max_artifact_age_seconds": 120.0,
                    "warn_artifact_age_seconds": 60.0,
                    "max_read_ms": 20.0,
                    "hard_gate": True,
                },
                "paper_router_preview": {
                    "json_path": str(base / "router.json"),
                    "required": True,
                    "execution_mode": "paper_only_router_artifact",
                    "max_artifact_age_seconds": 120.0,
                    "warn_artifact_age_seconds": 60.0,
                    "max_read_ms": 20.0,
                    "hard_gate": True,
                },
                "broker_margin_whatif_cache": {
                    "json_path": str(base / "whatif.json"),
                    "required": False,
                    "execution_mode": "precomputed_broker_whatif_cache",
                    "max_artifact_age_seconds": 86400.0,
                    "warn_artifact_age_seconds": 300.0,
                    "recommended_operational_max_age_seconds": 60.0,
                    "max_read_ms": 20.0,
                    "hard_gate": False,
                },
            },
        }
    }


def _loop_summary() -> dict:
    return {
        "tick_summaries": [
            {
                "elapsed_seconds": 0.24,
                "pending_paper_execution_count": 0,
                "release_governed_plan_blocked_count": 10,
                "market_data_operating_mode": "risk_reduction_only",
                "session_transition_release_status": "blocked",
                "session_transition_release_allowed": False,
                "pre_snapshot_refresh": {
                    "release_governed_recovery_closed_loop": {
                        "status": "pass",
                        "duration_seconds": 0.04,
                        "release_governor_reused": True,
                    }
                },
            }
        ]
    }


def _write_green_artifacts(base: Path) -> None:
    _write(base / "supervisor.json", {"final_status": "green", "final_source_ingest_health_status": "green", "safety": {"ibkr_order_submitted": False, "transmit_enabled": False}})
    _write(base / "market.json", {"status": "yellow", "summary": {"delayed_count": 0}, "safety": {"ibkr_order_submitted": False, "transmit_enabled": False, "account_or_portfolio_mutated": False}})
    _write(base / "release.json", {"release_status": "blocked", "ordinary_alpha_release_allowed": False, "safety": {"ibkr_order_submitted": False, "transmit_enabled": False, "live_order_routing_enabled": False, "portfolio_mutated": False, "scorer_rows_mutated": False}})
    _write(base / "router.json", {"paper_order_count": 0, "paper_fill_count": 0, "safety": {"ibkr_order_submitted": False, "transmit_enabled": False, "live_order_routing_enabled": False}})
    _write(base / "whatif.json", {"requested_count": 1, "response_count": 1, "safety": {"ibkr_order_api_called": True, "what_if_only": True, "transmit": False, "ibkr_order_submitted": False}})


def test_execution_hot_path_latency_audit_passes_fast_safe_path(tmp_path: Path) -> None:
    _write_green_artifacts(tmp_path)

    report = build_execution_hot_path_latency_audit(profile_payload=_profile(tmp_path), loop_summary_payload=_loop_summary())

    assert report["execution_hot_path_latency_audit_version"] == "execution-hot-path-latency-audit-v1"
    assert report["status"] == "pass"
    assert report["violations"] == []
    assert report["latency_summary"]["latest_tick_elapsed_seconds"] == 0.24
    assert report["latency_summary"]["release_governed_pass_seen"] is True
    assert report["safety_invariants"]["hot_path_audit_submits_orders"] is False

    markdown = execution_hot_path_latency_audit_markdown(report)
    assert "Execution Hot Path Latency Audit" in markdown
    assert write_execution_hot_path_latency_audit_json(tmp_path / "audit.json", report).exists()
    assert write_execution_hot_path_latency_audit_markdown(tmp_path / "audit.md", report).exists()


def test_execution_hot_path_latency_audit_flags_stale_required_artifact(tmp_path: Path) -> None:
    _write_green_artifacts(tmp_path)
    _write(tmp_path / "release.json", {"release_status": "blocked", "safety": {"ibkr_order_submitted": False}}, age_seconds=3600.0)

    report = build_execution_hot_path_latency_audit(profile_payload=_profile(tmp_path), loop_summary_payload=_loop_summary())

    assert report["status"] == "review"
    assert "session_transition_release_governor:required_artifact_stale" in report["violations"]


def test_execution_hot_path_latency_audit_flags_unsafe_broker_preview(tmp_path: Path) -> None:
    _write_green_artifacts(tmp_path)
    _write(tmp_path / "whatif.json", {"requested_count": 1, "safety": {"ibkr_order_api_called": True, "what_if_only": False, "transmit": True, "ibkr_order_submitted": False}})

    report = build_execution_hot_path_latency_audit(profile_payload=_profile(tmp_path), loop_summary_payload=_loop_summary())

    assert report["status"] == "review"
    assert "broker_margin_whatif_cache:broker_api_called_without_what_if_only" in report["violations"]
    assert "broker_margin_whatif_cache:transmit_true" in report["violations"]


def test_execution_hot_path_latency_audit_from_paths_uses_default_loop_summary(tmp_path: Path) -> None:
    _write_green_artifacts(tmp_path)
    profile = _profile(tmp_path)
    profile["default_inputs"] = {"loop_summary_json": str(tmp_path / "summary.json")}
    profile_path = _write(tmp_path / "profile.json", profile)
    _write(tmp_path / "summary.json", _loop_summary())

    report = build_execution_hot_path_latency_audit_from_paths(profile_json_path=profile_path)

    assert report["status"] == "pass"
    assert report["latency_summary"]["observed_tick_count"] == 1
