from __future__ import annotations

from .cli_replay_dataset_commands import add_replay_dataset_commands
from .cli_replay_score_commands import add_replay_score_commands
from .cli_replay_paper_score_commands import add_replay_paper_score_commands
from .cli_replay_strategy_commands import add_replay_strategy_commands
from .cli_replay_execution_safety_commands import add_replay_execution_safety_commands
from .cli_replay_paper_cycle_commands import add_replay_paper_cycle_commands


def add_replay_execution_commands(sub):
    add_replay_dataset_commands(sub)
    add_replay_score_commands(sub)
    add_replay_paper_score_commands(sub)
    add_replay_strategy_commands(sub)
    add_replay_execution_safety_commands(sub)
    add_replay_paper_cycle_commands(sub)
