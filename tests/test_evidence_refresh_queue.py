from __future__ import annotations

from pathlib import Path

from recorder.evidence_operations import (
    EvidenceRefreshQueueConfig,
    build_evidence_refresh_jobs,
    build_evidence_refresh_queue,
    evidence_refresh_queue_markdown,
)


class _Settings:
    watch_symbols = ("MU", "NVDA")
    benchmark_symbols = ("SPY", "QQQ")
    geopolitical_news_symbols = ("SPY", "QQQ", "GLD", "XLE")
    news_provider_codes = ("BZ",)

    @property
    def all_news_symbols(self) -> tuple[str, ...]:
        return ("MU", "NVDA", "SPY", "QQQ", "GLD", "XLE")

    @property
    def all_price_symbols(self) -> tuple[str, ...]:
        return ("MU", "NVDA", "SPY", "QQQ", "GLD", "XLE")


def _freshness_report() -> dict:
    return {
        "overall_status": "yellow",
        "stale_source_keys": ["market_news", "geopolitical_signals", "market_data"],
        "lagging_source_keys": ["standing_regimes", "technical_indicators"],
        "sources": [
            {"source_key": "market_news", "status": "stale", "latest_event_at": "2026-06-28T06:50:06+00:00"},
            {"source_key": "geopolitical_signals", "status": "stale", "latest_event_at": "2026-06-28T06:50:06+00:00"},
            {"source_key": "market_data", "status": "stale", "latest_event_at": "2026-06-30T07:40:15+00:00"},
            {"source_key": "standing_regimes", "status": "lagging", "latest_event_at": "2026-06-28T06:50:06+00:00"},
            {"source_key": "technical_indicators", "status": "lagging", "latest_event_at": "2026-06-26"},
            {"source_key": "economic_indicators", "status": "fresh", "latest_event_at": "2026-06-29"},
        ],
    }


def _config(tmp_path: Path, **overrides) -> EvidenceRefreshQueueConfig:
    kwargs = {
        "freshness_report_path": tmp_path / "freshness.json",
        "evidence_freshness_profile_path": Path("config/evidence_freshness_desk_profile_v1.json"),
        "dashboard_profile_path": Path("config/trading_dashboard_snapshot_profile_v1.json"),
        "output_dir": tmp_path,
        "execute": False,
    }
    kwargs.update(overrides)
    return EvidenceRefreshQueueConfig(**kwargs)


def test_evidence_refresh_queue_plans_jobs_for_stale_sources(tmp_path: Path) -> None:
    jobs = build_evidence_refresh_jobs(
        freshness_report=_freshness_report(),
        settings=_Settings(),
        config=_config(tmp_path),
    )
    job_ids = [job["job_id"] for job in jobs]

    assert "market_data_fresh_tick" in job_ids
    assert "ibkr_news_gap_backfill" in job_ids
    assert "external_market_sync" in job_ids
    assert "standing_regime_rebuild" in job_ids
    assert "technical_indicator_refresh" in job_ids
    assert job_ids[-2:] == ["evidence_freshness_recheck", "dashboard_rebuild"]
    assert all("sk-" not in " ".join(job["command"]) for job in jobs)


def test_evidence_refresh_queue_skip_live_and_max_jobs(tmp_path: Path) -> None:
    result = build_evidence_refresh_queue(
        freshness_report=_freshness_report(),
        settings=_Settings(),
        config=_config(tmp_path, include_live_ibkr=False, max_jobs=3),
        cwd=tmp_path,
    )
    markdown = evidence_refresh_queue_markdown(result)
    statuses = {job["job_id"]: job["status"] for job in result["jobs"]}

    assert statuses["market_data_fresh_tick"] == "skipped_live_ibkr_disabled"
    assert statuses["ibkr_news_gap_backfill"] == "skipped_live_ibkr_disabled"
    assert "external_market_sync" in statuses
    assert result["safety"]["ibkr_order_submitted"] is False
    assert "Evidence Refresh Queue" in markdown
