from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any


HORIZON_ORDER = {
    "30M": 30,
    "2H": 120,
    "1D": 1440,
    "5D": 7200,
}

SOURCE_ORDER_PREFIXES = (
    "ibkr_historical_trades_5_mins",
    "ibkr_historical_trades_daily",
    "yahoo_chart_daily",
    "stooq_daily",
)


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def _dash(value: Any) -> str:
    if value in (None, ""):
        return "-"
    return str(value)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _source_sort_key(source: str | None) -> tuple[int, str]:
    if not source:
        return (999, "")
    for index, prefix in enumerate(SOURCE_ORDER_PREFIXES):
        if source.startswith(prefix):
            return (index, source)
    return (500, source)


def _horizon_sort_key(horizon: str | None) -> tuple[int, str]:
    if not horizon:
        return (999999, "")
    normalized = horizon.upper()
    return (HORIZON_ORDER.get(normalized, 500000), normalized)


@dataclass(frozen=True)
class ReplayOutcomeComparison:
    summary: list[dict[str, Any]]
    events: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "events": self.events}


class ReplayOutcomeComparisonReporter:
    """Builds comparison summaries across stored replay outcome price sources."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def build(self) -> ReplayOutcomeComparison:
        return ReplayOutcomeComparison(
            summary=self._build_summary(),
            events=self._build_events(),
        )

    def _build_summary(self) -> list[dict[str, Any]]:
        buckets: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self.rows:
            if not row.get("outcome_id"):
                continue
            key = (str(row["price_source"]), str(row["horizon"]))
            bucket = buckets.setdefault(
                key,
                {
                    "price_source": row["price_source"],
                    "horizon": row["horizon"],
                    "rows": 0,
                    "complete": 0,
                    "blocked": 0,
                    "up": 0,
                    "down": 0,
                    "flat": 0,
                    "unknown": 0,
                    "_raw_returns": [],
                    "_excess_returns": [],
                },
            )
            bucket["rows"] += 1
            status = row.get("price_status") or "unknown"
            if status == "complete":
                bucket["complete"] += 1
                if row.get("raw_return") is not None:
                    bucket["_raw_returns"].append(float(row["raw_return"]))
                if row.get("excess_return") is not None:
                    bucket["_excess_returns"].append(float(row["excess_return"]))
            else:
                bucket["blocked"] += 1
            direction = row.get("direction_label") or "unknown"
            if direction in {"up", "down", "flat"}:
                bucket[direction] += 1
            else:
                bucket["unknown"] += 1

        summary = []
        for bucket in buckets.values():
            raw_returns = bucket.pop("_raw_returns")
            excess_returns = bucket.pop("_excess_returns")
            bucket["avg_raw_return"] = _mean(raw_returns)
            bucket["avg_excess_return"] = _mean(excess_returns)
            summary.append(bucket)
        summary.sort(key=lambda item: (_source_sort_key(item["price_source"]), _horizon_sort_key(item["horizon"])))
        return summary

    def _build_events(self) -> list[dict[str, Any]]:
        grouped: dict[int, dict[str, Any]] = {}
        outcomes: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            event_id = int(row["replay_corpus_id"])
            grouped.setdefault(
                event_id,
                {
                    "replay_corpus_id": event_id,
                    "symbol": row["actual_subject_symbol"],
                    "event_type": row["event_type"],
                    "event_subtype": row["event_subtype"],
                    "replay_timestamp": row["replay_timestamp"],
                    "session": row["session"],
                },
            )
            if not row.get("outcome_id"):
                continue
            outcomes[event_id].append(
                {
                    "outcome_id": row["outcome_id"],
                    "price_source": row["price_source"],
                    "horizon": row["horizon"],
                    "price_status": row["price_status"],
                    "baseline_at": row["baseline_at"],
                    "reaction_at": row["reaction_at"],
                    "baseline_price": row["baseline_price"],
                    "reaction_price": row["reaction_price"],
                    "raw_return": row["raw_return"],
                    "benchmark_symbol": row["benchmark_symbol"],
                    "benchmark_return": row["benchmark_return"],
                    "excess_return": row["excess_return"],
                    "direction_label": row["direction_label"],
                    "blocker_reason": row["blocker_reason"],
                }
            )

        events = []
        for event_id, event in grouped.items():
            event_outcomes = sorted(
                outcomes[event_id],
                key=lambda item: (_source_sort_key(item["price_source"]), _horizon_sort_key(item["horizon"])),
            )
            event["outcomes"] = event_outcomes
            events.append(event)
        events.sort(key=lambda item: (item["replay_timestamp"], item["replay_corpus_id"]), reverse=True)
        return events


def format_comparison_text(comparison: ReplayOutcomeComparison) -> str:
    lines: list[str] = []
    if not comparison.summary:
        lines.append("No replay market outcomes are stored for this comparison.")
        return "\n".join(lines)

    lines.append("Replay outcome comparison summary:")
    for item in comparison.summary:
        lines.append(
            f"- {item['price_source']} {item['horizon']}: "
            f"rows={item['rows']} complete={item['complete']} blocked={item['blocked']} "
            f"up/down/flat/unknown={item['up']}/{item['down']}/{item['flat']}/{item['unknown']} "
            f"avg_raw={_pct(item['avg_raw_return'])} avg_excess={_pct(item['avg_excess_return'])}"
        )

    lines.append("")
    lines.append("Recent event comparison:")
    for event in comparison.events:
        lines.append(
            f"#{event['replay_corpus_id']} {event['symbol']} "
            f"{event['event_type']}/{event['event_subtype']} "
            f"replay={event['replay_timestamp']} session={event['session']}"
        )
        if not event["outcomes"]:
            lines.append("  no stored outcomes")
            continue
        for outcome in event["outcomes"]:
            reason = f" blocker={outcome['blocker_reason']}" if outcome["blocker_reason"] else ""
            lines.append(
                f"  {outcome['price_source']} {outcome['horizon']} "
                f"{outcome['price_status']} {_dash(outcome['baseline_at'])} -> {_dash(outcome['reaction_at'])} "
                f"raw={_pct(outcome['raw_return'])} excess={_pct(outcome['excess_return'])} "
                f"direction={outcome['direction_label'] or '-'}{reason}"
            )
    return "\n".join(lines)
