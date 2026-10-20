from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

UTC = timezone.utc


def _float(name: str, default: float, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    value = float(raw) if raw is not None and raw.strip() else default
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.getenv(name)
    value = int(raw) if raw is not None and raw.strip() else default
    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def parse_as_of(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class EvaluationConfig:
    database_path: Path
    policy_version: str = "evaluation-policy-v1"
    code_version: str = "0.1.1"
    brier_definition: str = "multiclass_sum"
    probability_clip: float = 1e-12
    calibration_bins: int = 10
    calibration_min_samples: int = 20
    calibration_detailed_min_samples: int = 50
    calibration_shrinkage_strength: float = 25.0
    flat_band_15m_bps: float = 15.0
    flat_band_1h_bps: float = 25.0
    flat_band_close_bps: float = 35.0
    flat_band_1d_bps: float = 50.0
    flat_band_3d_bps: float = 100.0

    @classmethod
    def from_env(cls, database_path: str | Path) -> "EvaluationConfig":
        return cls(
            database_path=Path(database_path),
            policy_version=os.getenv("EVALUATION_POLICY_VERSION", "evaluation-policy-v1").strip(),
            code_version=os.getenv("EVALUATION_CODE_VERSION", "0.1.1").strip(),
            probability_clip=_float("EVALUATION_PROBABILITY_CLIP", 1e-12, 1e-15),
            calibration_bins=_int("CALIBRATION_BIN_COUNT", 10, 2),
            calibration_min_samples=_int("CALIBRATION_MIN_SAMPLES", 20, 1),
            calibration_detailed_min_samples=_int(
                "CALIBRATION_DETAILED_MIN_SAMPLES", 50, 1
            ),
            calibration_shrinkage_strength=_float(
                "CALIBRATION_SHRINKAGE_STRENGTH", 25.0, 0.0
            ),
            flat_band_15m_bps=_float("EVALUATION_FLAT_BAND_15M_BPS", 15.0, 0.0),
            flat_band_1h_bps=_float("EVALUATION_FLAT_BAND_1H_BPS", 25.0, 0.0),
            flat_band_close_bps=_float("EVALUATION_FLAT_BAND_CLOSE_BPS", 35.0, 0.0),
            flat_band_1d_bps=_float("EVALUATION_FLAT_BAND_1D_BPS", 50.0, 0.0),
            flat_band_3d_bps=_float("EVALUATION_FLAT_BAND_3D_BPS", 100.0, 0.0),
        )

    def flat_threshold(self, horizon: str) -> float:
        normalized = horizon.strip().upper()
        bps = {
            "15M": self.flat_band_15m_bps,
            "1H": self.flat_band_1h_bps,
            "CLOSE": self.flat_band_close_bps,
            "1D": self.flat_band_1d_bps,
            "3D": self.flat_band_3d_bps,
        }.get(normalized)
        if bps is None:
            raise KeyError(f"Unsupported evaluation horizon: {horizon}")
        return bps / 10_000.0
