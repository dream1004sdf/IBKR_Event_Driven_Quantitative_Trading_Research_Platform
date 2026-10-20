from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_profile_json(profile_json_path: Path | None) -> tuple[dict[str, Any], Path | None]:
    if profile_json_path is None:
        return {}, None
    profile_json_path = profile_json_path.expanduser().resolve()
    return json.loads(profile_json_path.read_text(encoding="utf-8")), profile_json_path.parent


def profile_lookup(profile: dict[str, Any], section: str, key: str, default: Any = None) -> Any:
    section_value = profile.get(section)
    if isinstance(section_value, dict) and key in section_value:
        return section_value.get(key)
    if key in profile:
        return profile.get(key)
    return default


def coalesce_profile_value(
    cli_value: Any,
    profile: dict[str, Any],
    section: str,
    key: str,
    default: Any = None,
) -> Any:
    return cli_value if cli_value is not None else profile_lookup(profile, section, key, default)


def coalesce_profile_bool(
    cli_value: bool | None,
    profile: dict[str, Any],
    section: str,
    key: str,
    default: bool,
) -> bool:
    value = coalesce_profile_value(cli_value, profile, section, key, default)
    return bool(value)


def profile_path_value(value: Any, profile_base: Path | None) -> Path | None:
    if value in (None, ""):
        return None
    path = value if isinstance(value, Path) else Path(str(value))
    if not path.is_absolute() and profile_base is not None:
        path = profile_base / path
    return path


def coalesce_profile_path(
    cli_value: Path | None,
    profile: dict[str, Any],
    section: str,
    key: str,
    profile_base: Path | None,
) -> Path | None:
    if cli_value is not None:
        return cli_value
    return profile_path_value(profile_lookup(profile, section, key), profile_base)


def profile_path_list_value(value: Any, profile_base: Path | None) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, list):
        paths = [profile_path_value(item, profile_base) for item in value]
        return ",".join(str(path) for path in paths if path is not None)
    if isinstance(value, str) and "," in value:
        paths = [profile_path_value(item.strip(), profile_base) for item in value.split(",") if item.strip()]
        return ",".join(str(path) for path in paths if path is not None)
    return str(profile_path_value(value, profile_base) or "")


def coalesce_profile_path_list(
    cli_value: str | None,
    profile: dict[str, Any],
    section: str,
    key: str,
    profile_base: Path | None,
) -> str | None:
    if cli_value is not None:
        return cli_value
    return profile_path_list_value(profile_lookup(profile, section, key), profile_base)
