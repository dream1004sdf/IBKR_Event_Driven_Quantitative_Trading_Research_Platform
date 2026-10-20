from __future__ import annotations

import json
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Callable, Iterable, Mapping, Sequence

from .config import EvaluationConfig
from .db import EvaluationDatabase, utc_iso

UTC = timezone.utc
CLASS_COLUMNS = {"up": "p_up", "flat": "p_flat", "down": "p_down"}


def _clip(value: float, epsilon: float = 1e-9) -> float:
    return min(1.0 - epsilon, max(epsilon, value))


def _sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1.0 / (1.0 + z)
    z = math.exp(value)
    return z / (1.0 + z)


def _binary_metrics(probabilities: Sequence[float], labels: Sequence[int], bins: int = 10) -> dict[str, float | int | None]:
    if not probabilities:
        return {"count": 0, "brier": None, "log_loss": None, "ece": None, "mean_probability": None, "actual_rate": None}
    brier = mean((p - y) ** 2 for p, y in zip(probabilities, labels))
    log_loss = mean(-(y * math.log(_clip(p)) + (1 - y) * math.log(_clip(1 - p))) for p, y in zip(probabilities, labels))
    ece = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        selected = [i for i, p in enumerate(probabilities) if low <= p < high or (index == bins - 1 and p == 1.0)]
        if not selected:
            continue
        avg_p = mean(probabilities[i] for i in selected)
        rate = mean(labels[i] for i in selected)
        ece += len(selected) / len(probabilities) * abs(avg_p - rate)
    return {
        "count": len(probabilities),
        "brier": brier,
        "log_loss": log_loss,
        "ece": ece,
        "mean_probability": mean(probabilities),
        "actual_rate": mean(labels),
    }


def _fit_bin_shrinkage(
    probabilities: Sequence[float], labels: Sequence[int], *, bins: int, strength: float
) -> dict[str, Any]:
    edges = [index / bins for index in range(bins + 1)]
    entries: list[dict[str, float | int]] = []
    for index in range(bins):
        low, high = edges[index], edges[index + 1]
        selected = [i for i, p in enumerate(probabilities) if low <= p < high or (index == bins - 1 and p == 1.0)]
        if selected:
            avg_raw = mean(probabilities[i] for i in selected)
            empirical = mean(labels[i] for i in selected)
            n = len(selected)
            weight = n / (n + strength) if strength > 0 else 1.0
            calibrated = weight * empirical + (1.0 - weight) * avg_raw
        else:
            avg_raw = (low + high) / 2.0
            empirical = avg_raw
            n = 0
            calibrated = avg_raw
        entries.append(
            {
                "low": low,
                "high": high,
                "count": n,
                "avg_raw": avg_raw,
                "empirical_rate": empirical,
                "calibrated_probability": calibrated,
            }
        )
    return {"bins": entries, "strength": strength}


def _apply_bin(parameters: Mapping[str, Any], probability: float) -> float:
    for entry in parameters.get("bins", []):
        low, high = float(entry["low"]), float(entry["high"])
        if low <= probability < high or (high == 1.0 and probability == 1.0):
            return _clip(float(entry["calibrated_probability"]))
    return _clip(probability)


def _fit_isotonic(probabilities: Sequence[float], labels: Sequence[int]) -> dict[str, Any]:
    pairs = sorted(zip(probabilities, labels), key=lambda item: item[0])
    blocks: list[dict[str, float]] = []
    for p, y in pairs:
        blocks.append({"min_p": p, "max_p": p, "sum_y": float(y), "weight": 1.0})
        while len(blocks) >= 2:
            left, right = blocks[-2], blocks[-1]
            left_value = left["sum_y"] / left["weight"]
            right_value = right["sum_y"] / right["weight"]
            if left_value <= right_value:
                break
            merged = {
                "min_p": left["min_p"],
                "max_p": right["max_p"],
                "sum_y": left["sum_y"] + right["sum_y"],
                "weight": left["weight"] + right["weight"],
            }
            blocks[-2:] = [merged]
    return {
        "blocks": [
            {"min_p": block["min_p"], "max_p": block["max_p"], "value": block["sum_y"] / block["weight"], "count": int(block["weight"])}
            for block in blocks
        ]
    }


def _apply_isotonic(parameters: Mapping[str, Any], probability: float) -> float:
    blocks = parameters.get("blocks", [])
    if not blocks:
        return _clip(probability)
    for block in blocks:
        if probability <= float(block["max_p"]):
            return _clip(float(block["value"]))
    return _clip(float(blocks[-1]["value"]))


def _features(method: str, probability: float) -> list[float]:
    p = _clip(probability)
    if method == "platt":
        return [math.log(p / (1.0 - p)), 1.0]
    if method == "beta":
        return [math.log(p), math.log(1.0 - p), 1.0]
    raise ValueError(f"Unsupported logistic calibration method: {method}")


def _fit_logistic(probabilities: Sequence[float], labels: Sequence[int], method: str) -> dict[str, Any]:
    dimension = 2 if method == "platt" else 3
    weights = [0.0] * dimension
    learning_rate = 0.08
    ridge = 1e-4
    for iteration in range(2500):
        gradients = [0.0] * dimension
        for probability, label in zip(probabilities, labels):
            xs = _features(method, probability)
            predicted = _sigmoid(sum(w * x for w, x in zip(weights, xs)))
            error = predicted - label
            for index, x in enumerate(xs):
                gradients[index] += error * x
        scale = 1.0 / max(1, len(probabilities))
        step = learning_rate / math.sqrt(1.0 + iteration / 250.0)
        max_change = 0.0
        for index in range(dimension):
            gradient = gradients[index] * scale + ridge * weights[index]
            change = step * gradient
            weights[index] -= change
            max_change = max(max_change, abs(change))
        if max_change < 1e-8:
            break
    return {"weights": weights, "method": method}


def _apply_logistic(parameters: Mapping[str, Any], probability: float) -> float:
    method = str(parameters["method"])
    weights = [float(value) for value in parameters["weights"]]
    xs = _features(method, probability)
    return _clip(_sigmoid(sum(w * x for w, x in zip(weights, xs))))


def _apply(method: str, parameters: Mapping[str, Any], probability: float) -> float:
    if method == "identity":
        return _clip(probability)
    if method == "bin_shrinkage":
        return _apply_bin(parameters, probability)
    if method == "isotonic":
        return _apply_isotonic(parameters, probability)
    if method in {"platt", "beta"}:
        return _apply_logistic(parameters, probability)
    raise ValueError(f"Unsupported calibration method: {method}")


@dataclass(frozen=True)
class CalibrationFitResult:
    version: str
    profile_count: int
    training_count: int
    validation_count: int
    method_requested: str

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "profile_count": self.profile_count,
            "training_count": self.training_count,
            "validation_count": self.validation_count,
            "method_requested": self.method_requested,
        }


class CalibrationEngine:
    def __init__(self, config: EvaluationConfig):
        self.config = config
        self.db = EvaluationDatabase(config.database_path)
        self.db.migrate()

    def _rows(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT * FROM prediction_outcomes
                    WHERE policy_version=? AND eligibility_status='evaluated'
                    ORDER BY prediction_as_of,id
                    """,
                    (self.config.policy_version,),
                ).fetchall()
            ]

    @staticmethod
    def _time_split(rows: Sequence[Mapping[str, Any]], training_fraction: float = 0.8) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        materialized = [dict(row) for row in rows]
        if len(materialized) < 10:
            return materialized, []
        split_index = max(1, min(len(materialized) - 1, int(len(materialized) * training_fraction)))
        return materialized[:split_index], materialized[split_index:]

    @staticmethod
    def _group_specs(rows: Sequence[Mapping[str, Any]], min_samples: int, detailed_min: int) -> list[tuple[str, dict[str, str | None], list[dict[str, Any]]]]:
        specs: list[tuple[str, dict[str, str | None], list[dict[str, Any]]]] = [
            ("global", {}, [dict(row) for row in rows])
        ]

        def add_groups(level: str, keys: tuple[str, ...], threshold: int) -> None:
            groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
            for raw in rows:
                row = dict(raw)
                key = tuple(str(row.get(column) or "") for column in keys)
                groups.setdefault(key, []).append(row)
            for key, selected in groups.items():
                if len(selected) < threshold:
                    continue
                dimensions = {column: value for column, value in zip(keys, key)}
                specs.append((level, dimensions, selected))

        add_groups("horizon", ("horizon",), min_samples)
        add_groups("event_horizon", ("event_type", "horizon"), min_samples)
        add_groups(
            "model_event_horizon",
            ("producer_type", "producer_name", "model_name", "event_type", "horizon"),
            detailed_min,
        )
        return specs

    def _select_method(self, requested: str, sample_count: int) -> str:
        if requested == "auto":
            return "identity" if sample_count < self.config.calibration_min_samples else "bin_shrinkage"
        normalized = requested.replace("-", "_")
        if normalized not in {"identity", "bin_shrinkage", "isotonic", "platt", "beta"}:
            raise ValueError(f"Unknown calibration method: {requested}")
        minimum = {
            "identity": 0,
            "bin_shrinkage": self.config.calibration_min_samples,
            "isotonic": max(30, self.config.calibration_min_samples),
            "platt": max(50, self.config.calibration_detailed_min_samples),
            "beta": max(50, self.config.calibration_detailed_min_samples),
        }[normalized]
        return normalized if sample_count >= minimum else "identity"

    def _fit_method(self, method: str, probabilities: Sequence[float], labels: Sequence[int]) -> dict[str, Any]:
        if method == "identity":
            return {}
        if method == "bin_shrinkage":
            return _fit_bin_shrinkage(
                probabilities,
                labels,
                bins=self.config.calibration_bins,
                strength=self.config.calibration_shrinkage_strength,
            )
        if method == "isotonic":
            return _fit_isotonic(probabilities, labels)
        if method in {"platt", "beta"}:
            return _fit_logistic(probabilities, labels, method)
        raise ValueError(method)

    def fit(self, *, method: str = "auto", training_fraction: float = 0.8) -> CalibrationFitResult:
        rows = self._rows()
        version = f"cal-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
        if not rows:
            return CalibrationFitResult(version, 0, 0, 0, method)
        training, validation = self._time_split(rows, training_fraction=training_fraction)
        training_ids = {int(row["id"]) for row in training}
        validation_ids = {int(row["id"]) for row in validation}
        specs = self._group_specs(
            training,
            self.config.calibration_min_samples,
            self.config.calibration_detailed_min_samples,
        )
        profile_count = 0
        with self.db.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE calibration_profiles SET status='superseded' WHERE policy_version=? AND status='active'",
                (self.config.policy_version,),
            )
            for level, dimensions, group_training in specs:
                def matches(row: Mapping[str, Any]) -> bool:
                    return all(str(row.get(key) or "") == str(value or "") for key, value in dimensions.items())

                group_validation = [row for row in validation if matches(row)]
                for target_class, probability_column in CLASS_COLUMNS.items():
                    probabilities = [float(row[probability_column]) for row in group_training]
                    labels = [1 if row["realized_direction"] == target_class else 0 for row in group_training]
                    selected_method = self._select_method(method, len(group_training))
                    parameters = self._fit_method(selected_method, probabilities, labels)
                    evaluation_rows = group_validation or group_training
                    raw_eval = [float(row[probability_column]) for row in evaluation_rows]
                    labels_eval = [1 if row["realized_direction"] == target_class else 0 for row in evaluation_rows]
                    calibrated_eval = [_apply(selected_method, parameters, probability) for probability in raw_eval]
                    metrics_before = _binary_metrics(raw_eval, labels_eval, bins=self.config.calibration_bins)
                    metrics_after = _binary_metrics(calibrated_eval, labels_eval, bins=self.config.calibration_bins)
                    profile_uuid = f"{version}-{level}-{target_class}-{uuid.uuid4().hex[:8]}"
                    values = {
                        "profile_uuid": profile_uuid,
                        "version": version,
                        "policy_version": self.config.policy_version,
                        "status": "active",
                        "method": selected_method,
                        "target_class": target_class,
                        "fallback_level": level,
                        "producer_type": dimensions.get("producer_type"),
                        "producer_name": dimensions.get("producer_name"),
                        "model_name": dimensions.get("model_name"),
                        "prompt_version": dimensions.get("prompt_version"),
                        "event_type": dimensions.get("event_type"),
                        "horizon": dimensions.get("horizon"),
                        "symbol": dimensions.get("symbol"),
                        "source_class": dimensions.get("source_class"),
                        "training_start": group_training[0]["prediction_as_of"] if group_training else None,
                        "training_end": group_training[-1]["prediction_as_of"] if group_training else None,
                        "validation_start": group_validation[0]["prediction_as_of"] if group_validation else None,
                        "validation_end": group_validation[-1]["prediction_as_of"] if group_validation else None,
                        "sample_count": len(group_training),
                        "validation_count": len(group_validation),
                        "bin_edges_json": json.dumps(
                            [index / self.config.calibration_bins for index in range(self.config.calibration_bins + 1)]
                            if selected_method == "bin_shrinkage" else [],
                            separators=(",", ":"),
                        ),
                        "parameters_json": json.dumps(parameters, separators=(",", ":"), sort_keys=True),
                        "metrics_before_json": json.dumps(metrics_before, separators=(",", ":"), sort_keys=True),
                        "metrics_after_json": json.dumps(metrics_after, separators=(",", ":"), sort_keys=True),
                        "created_at": utc_iso(),
                    }
                    columns = list(values)
                    conn.execute(
                        f"INSERT INTO calibration_profiles({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                        tuple(values[column] for column in columns),
                    )
                    profile_count += 1
            conn.execute("COMMIT")
        return CalibrationFitResult(version, profile_count, len(training_ids), len(validation_ids), method)

    def calibrate_probabilities(
        self,
        row: Mapping[str, Any],
        *,
        version: str | None = None,
    ) -> dict[str, Any]:
        """Apply the most specific active profile and renormalize the three classes.

        This method is the stable integration point for the later Signal Decision Engine.
        It never mutates the recorder prediction.
        """
        if version is None:
            with self.db.connect() as conn:
                latest = conn.execute(
                    "SELECT version FROM calibration_profiles WHERE policy_version=? AND status='active' ORDER BY id DESC LIMIT 1",
                    (self.config.policy_version,),
                ).fetchone()
            version = str(latest["version"]) if latest else None
        raw = {name: float(row[column]) for name, column in CLASS_COLUMNS.items()}
        total = sum(max(0.0, value) for value in raw.values())
        if total <= 0:
            raise ValueError("Raw probability vector has no positive mass")
        raw = {name: max(0.0, value) / total for name, value in raw.items()}
        if version is None:
            return {"version": None, "raw": raw, "calibrated": raw, "profiles": {}}

        with self.db.connect() as conn:
            profiles = [dict(item) for item in conn.execute(
                """
                SELECT * FROM calibration_profiles
                WHERE policy_version=? AND version=? AND status='active'
                ORDER BY id
                """,
                (self.config.policy_version, version),
            ).fetchall()]

        specificity = {"global": 0, "horizon": 1, "event_horizon": 2, "model_event_horizon": 3}

        def matches(profile: Mapping[str, Any]) -> bool:
            for key in ("producer_type", "producer_name", "model_name", "prompt_version", "event_type", "horizon", "symbol", "source_class"):
                expected = profile.get(key)
                if expected is not None and str(expected) != str(row.get(key) or ""):
                    return False
            return True

        calibrated: dict[str, float] = {}
        selected: dict[str, int] = {}
        for class_name in CLASS_COLUMNS:
            candidates = [profile for profile in profiles if profile["target_class"] == class_name and matches(profile)]
            candidates.sort(key=lambda profile: (specificity.get(str(profile["fallback_level"]), -1), int(profile["sample_count"]), int(profile["id"])), reverse=True)
            if not candidates:
                calibrated[class_name] = raw[class_name]
                continue
            profile = candidates[0]
            parameters = json.loads(profile["parameters_json"] or "{}")
            calibrated[class_name] = _apply(str(profile["method"]), parameters, raw[class_name])
            selected[class_name] = int(profile["id"])
        calibrated_total = sum(calibrated.values())
        if calibrated_total <= 0:
            calibrated = raw
        else:
            calibrated = {name: value / calibrated_total for name, value in calibrated.items()}
        return {"version": version, "raw": raw, "calibrated": calibrated, "profiles": selected}

    def report(self, *, version: str | None = None, event_type: str | None = None, horizon: str | None = None) -> dict[str, Any]:
        clauses = ["policy_version=?"]
        params: list[Any] = [self.config.policy_version]
        if version is None:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT version FROM calibration_profiles WHERE policy_version=? ORDER BY id DESC LIMIT 1",
                    (self.config.policy_version,),
                ).fetchone()
            version = str(row["version"]) if row else None
        if version is None:
            return {"policy_version": self.config.policy_version, "version": None, "profile_count": 0, "profiles": []}
        clauses.append("version=?")
        params.append(version)
        if event_type:
            clauses.append("event_type=?")
            params.append(event_type.lower())
        if horizon:
            clauses.append("horizon=?")
            params.append(horizon.upper())
        with self.db.connect() as conn:
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM calibration_profiles WHERE {' AND '.join(clauses)} ORDER BY fallback_level,target_class,id",
                params,
            ).fetchall()]
        profiles = []
        for row in rows:
            row["parameters"] = json.loads(row.pop("parameters_json") or "{}")
            row["metrics_before"] = json.loads(row.pop("metrics_before_json") or "{}")
            row["metrics_after"] = json.loads(row.pop("metrics_after_json") or "{}")
            row["bin_edges"] = json.loads(row.pop("bin_edges_json") or "[]")
            profiles.append(row)
        return {
            "policy_version": self.config.policy_version,
            "version": version,
            "profile_count": len(profiles),
            "profiles": profiles,
        }
