"""Run the existing recorder self-test using temporary, synthetic data."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recorder.self_test import run_self_test


if __name__ == '__main__':
    print(json.dumps(run_self_test(), indent=2))
