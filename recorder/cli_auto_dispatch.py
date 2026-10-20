from __future__ import annotations

import inspect
from typing import Any, Callable


def command_func_name(command_name: str) -> str:
    return "command_" + command_name.replace("-", "_")


def dispatch_by_signature(
    args: Any,
    settings: Any,
    commands: dict[str, Any],
    command_names: set[str],
    *,
    aliases: dict[str, str] | None = None,
    value_overrides: dict[str, Callable[[Any], Any]] | None = None,
) -> bool:
    """Dispatch argparse args to a command_* function using its signature.

    This keeps large CLI modules from repeating long `command_x(args.a, args.b, ...)`
    blocks in main.py.  It is intentionally small and conservative: only commands
    explicitly listed in command_names are handled.
    """

    command_name = getattr(args, "command", None)
    if command_name not in command_names:
        return False

    function_name = command_func_name(command_name)
    func = commands.get(function_name)
    if func is None:
        raise SystemExit(f"Missing command function: {function_name}")

    aliases = aliases or {}
    value_overrides = value_overrides or {}
    values: list[Any] = []
    for parameter_name, parameter in inspect.signature(func).parameters.items():
        if parameter_name == "settings":
            values.append(settings)
            continue
        if parameter_name in value_overrides:
            values.append(value_overrides[parameter_name](args))
            continue
        arg_name = aliases.get(parameter_name, parameter_name)
        if not hasattr(args, arg_name):
            if parameter.default is not inspect.Parameter.empty:
                values.append(parameter.default)
                continue
            raise SystemExit(
                f"Cannot dispatch {command_name}: missing argparse value for {parameter_name!r}"
            )
        values.append(getattr(args, arg_name))
    func(*values)
    return True
