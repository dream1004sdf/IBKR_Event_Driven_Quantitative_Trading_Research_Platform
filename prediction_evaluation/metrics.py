from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Iterable, Mapping, Sequence

CLASSES = ("up", "flat", "down")


def normalize_probabilities(p_up: float, p_flat: float, p_down: float) -> tuple[float, float, float]:
    values = [max(0.0, float(p_up)), max(0.0, float(p_flat)), max(0.0, float(p_down))]
    total = sum(values)
    if total <= 0 or not all(math.isfinite(value) for value in values):
        raise ValueError("Probability vector must contain finite positive mass")
    return tuple(value / total for value in values)  # type: ignore[return-value]


def predicted_direction(p_up: float, p_flat: float, p_down: float) -> str:
    probabilities = normalize_probabilities(p_up, p_flat, p_down)
    # Deterministic tie-breaking favors flat, then the direction with larger probability.
    ranked = sorted(zip(CLASSES, probabilities), key=lambda item: (item[1], item[0] == "flat"), reverse=True)
    return ranked[0][0]


def realized_direction(realized_return: float, flat_threshold: float) -> str:
    if realized_return > flat_threshold:
        return "up"
    if realized_return < -flat_threshold:
        return "down"
    return "flat"


def confidence_bucket(confidence: float) -> str:
    value = float(confidence)
    if value < 0.40:
        return "<40"
    if value < 0.50:
        return "40-50"
    if value < 0.60:
        return "50-60"
    if value < 0.70:
        return "60-70"
    return "70+"


def safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx, my = mean(xs), mean(ys)
    numerator = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return numerator / (dx * dy)


@dataclass(frozen=True)
class MetricSummary:
    sample_count: int
    direction_accuracy: float | None
    precision: dict[str, float | None]
    recall: dict[str, float | None]
    f1: dict[str, float | None]
    confusion_matrix: dict[str, dict[str, int]]
    brier_score: float | None
    log_loss: float | None
    calibration_error: float | None
    reliability_curve: dict[str, list[dict[str, float | int | None]]]
    mae: float | None
    rmse: float | None
    bias: float | None
    return_correlation: float | None
    scenario_range_coverage: float | None

    def as_dict(self) -> dict[str, object]:
        return {
            "sample_count": self.sample_count,
            "direction_accuracy": self.direction_accuracy,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "confusion_matrix": self.confusion_matrix,
            "brier_score": self.brier_score,
            "log_loss": self.log_loss,
            "calibration_error": self.calibration_error,
            "reliability_curve": self.reliability_curve,
            "mae": self.mae,
            "rmse": self.rmse,
            "bias": self.bias,
            "return_correlation": self.return_correlation,
            "scenario_range_coverage": self.scenario_range_coverage,
        }


def _row_value(row: Mapping[str, object], key: str) -> object:
    value = row[key]
    if value is None:
        raise ValueError(f"Metric row is missing {key}")
    return value


def reliability_curve(
    rows: Sequence[Mapping[str, object]], bins: int = 10
) -> tuple[dict[str, list[dict[str, float | int | None]]], float | None]:
    if not rows:
        return {name: [] for name in CLASSES}, None
    result: dict[str, list[dict[str, float | int | None]]] = {}
    class_eces: list[float] = []
    for class_name, probability_key in zip(CLASSES, ("p_up", "p_flat", "p_down")):
        bucket_rows: list[dict[str, float | int | None]] = []
        weighted_gap = 0.0
        for index in range(bins):
            low = index / bins
            high = (index + 1) / bins
            selected = []
            for row in rows:
                probability = float(_row_value(row, probability_key))
                in_bucket = low <= probability < high or (index == bins - 1 and probability == 1.0)
                if in_bucket:
                    selected.append(row)
            if selected:
                avg_probability = mean(float(_row_value(row, probability_key)) for row in selected)
                actual_rate = mean(1.0 if row["realized_direction"] == class_name else 0.0 for row in selected)
                gap = abs(avg_probability - actual_rate)
                weighted_gap += len(selected) / len(rows) * gap
            else:
                avg_probability = None
                actual_rate = None
                gap = None
            bucket_rows.append(
                {
                    "low": low,
                    "high": high,
                    "count": len(selected),
                    "avg_probability": avg_probability,
                    "actual_rate": actual_rate,
                    "absolute_gap": gap,
                }
            )
        result[class_name] = bucket_rows
        class_eces.append(weighted_gap)
    return result, mean(class_eces) if class_eces else None


def calculate_metrics(
    rows: Iterable[Mapping[str, object]], *, probability_clip: float = 1e-12, bins: int = 10
) -> MetricSummary:
    materialized = list(rows)
    count = len(materialized)
    matrix = {actual: {predicted: 0 for predicted in CLASSES} for actual in CLASSES}
    if not materialized:
        return MetricSummary(
            0,
            None,
            {name: None for name in CLASSES},
            {name: None for name in CLASSES},
            {name: None for name in CLASSES},
            matrix,
            None,
            None,
            None,
            {name: [] for name in CLASSES},
            None,
            None,
            None,
            None,
            None,
        )

    correct = 0
    brier_values: list[float] = []
    log_losses: list[float] = []
    absolute_errors: list[float] = []
    squared_errors: list[float] = []
    signed_errors: list[float] = []
    expected_returns: list[float] = []
    realized_returns: list[float] = []
    range_hits: list[float] = []

    for row in materialized:
        actual = str(_row_value(row, "realized_direction"))
        predicted = str(_row_value(row, "predicted_direction"))
        if actual not in CLASSES or predicted not in CLASSES:
            raise ValueError(f"Invalid direction pair: actual={actual}, predicted={predicted}")
        matrix[actual][predicted] += 1
        correct += int(actual == predicted)

        probabilities = normalize_probabilities(
            float(_row_value(row, "p_up")),
            float(_row_value(row, "p_flat")),
            float(_row_value(row, "p_down")),
        )
        brier_values.append(
            sum((probability - (1.0 if class_name == actual else 0.0)) ** 2 for class_name, probability in zip(CLASSES, probabilities))
        )
        actual_probability = probabilities[CLASSES.index(actual)]
        actual_probability = min(1.0 - probability_clip, max(probability_clip, actual_probability))
        log_losses.append(-math.log(actual_probability))

        expected = float(_row_value(row, "expected_return"))
        realized = float(_row_value(row, "realized_return"))
        signed = expected - realized
        expected_returns.append(expected)
        realized_returns.append(realized)
        signed_errors.append(signed)
        absolute_errors.append(abs(signed))
        squared_errors.append(signed**2)
        if row.get("scenario_range_hit") is not None:
            range_hits.append(float(row["scenario_range_hit"]))

    precision: dict[str, float | None] = {}
    recall: dict[str, float | None] = {}
    f1: dict[str, float | None] = {}
    for class_name in CLASSES:
        tp = matrix[class_name][class_name]
        predicted_total = sum(matrix[actual][class_name] for actual in CLASSES)
        actual_total = sum(matrix[class_name].values())
        precision[class_name] = safe_div(tp, predicted_total)
        recall[class_name] = safe_div(tp, actual_total)
        if precision[class_name] is None or recall[class_name] is None or precision[class_name] + recall[class_name] == 0:
            f1[class_name] = None
        else:
            f1[class_name] = 2 * precision[class_name] * recall[class_name] / (precision[class_name] + recall[class_name])

    reliability, ece = reliability_curve(materialized, bins=bins)
    return MetricSummary(
        sample_count=count,
        direction_accuracy=correct / count,
        precision=precision,
        recall=recall,
        f1=f1,
        confusion_matrix=matrix,
        brier_score=mean(brier_values),
        log_loss=mean(log_losses),
        calibration_error=ece,
        reliability_curve=reliability,
        mae=mean(absolute_errors),
        rmse=math.sqrt(mean(squared_errors)),
        bias=mean(signed_errors),
        return_correlation=pearson(expected_returns, realized_returns),
        scenario_range_coverage=mean(range_hits) if range_hits else None,
    )


def json_compact(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
