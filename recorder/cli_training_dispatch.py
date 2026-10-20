from __future__ import annotations

from typing import Any

from .cli_auto_dispatch import dispatch_by_signature


TRAINING_COMMANDS = {
    "live-scorer-learning",
    "live-learning-llm-notes",
    "threshold-candidate-evaluator",
    "m236-news-learning",
    "m236-outcome-attach",
    "macro-event-calendar",
    "macro-release-interpreter",
    "unknown-theme-discovery",
    "walk-forward-learning-sim",
    "walk-forward-adjustment-notes",
    "walk-forward-cycle-train",
    "walk-forward-cycle-threshold-tune",
    "walk-forward-cycle-nuanced-train",
}


def handle_training_command(args: Any, settings: Any, commands: dict[str, Any]) -> bool:
    return dispatch_by_signature(
        args,
        settings,
        commands,
        TRAINING_COMMANDS,
        aliases={
            "profile_json_path": "profile_json",
            "price_source_name": "price_source",
            "json_path": "json_output_path",
            "json_output": "json",
        },
    )
