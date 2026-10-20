from __future__ import annotations

import ast
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CODE_HEALTH_REPORT_VERSION = "code-health-report-v1"


def _group_for_path(path: Path) -> str:
    name = path.name
    text = path.as_posix()
    stem = path.stem
    if name == "main.py" or name.startswith("cli_"):
        if "paper_cycle" in name or "paper" in name:
            return "paper_execution"
        if "dashboard" in name:
            return "dashboard"
        if "options" in name or "option" in name:
            return "options"
        if "replay" in name:
            return "replay"
        if "market" in name:
            return "market_data"
        if "news" in name or "source" in name or "data" in name:
            return "sources_news"
        return "cli_entrypoints"
    if "options" in name or "option_" in name:
        return "options"
    if "dashboard" in name:
        return "dashboard"
    if "replay" in name:
        return "replay"
    if "analysis_database" in name or "database" in name or stem in {"db_backup", "analysis_db"}:
        return "database"
    if "learning" in name or "threshold" in name:
        return "learning"
    if "market_data" in name or "market_transition" in name:
        return "market_data"
    if "source" in name or "external" in name or "news" in name:
        return "sources_news"
    if "execution" in name or "operating" in name or "safety" in name or "risk" in name:
        return "execution_safety"
    if "ibkr" in name or "broker" in name or "account" in name:
        return "broker_ibkr"
    if "macro" in name or "economic" in name or "financial" in name or "technical" in name:
        return "structured_evidence"
    if text.startswith("prediction_evaluation/"):
        return "prediction_evaluation"
    return "core_other"


def _iter_python_files(root: Path, include_main: bool) -> list[Path]:
    files: list[Path] = []
    for folder in ("recorder", "prediction_evaluation"):
        folder_path = root / folder
        if folder_path.exists():
            files.extend(sorted(folder_path.glob("*.py")))
    if include_main and (root / "main.py").exists():
        files.append(root / "main.py")
    return files


def _analyze_file(path: Path, root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    rel = path.relative_to(root)
    text = path.read_text(encoding="utf-8")
    lines = text.count("\n") + 1
    tree = ast.parse(text)
    defs = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    imports = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))]
    long_functions: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        end = int(getattr(node, "end_lineno", node.lineno) or node.lineno)
        length = end - int(node.lineno) + 1
        branch_count = sum(
            isinstance(child, (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.BoolOp, ast.Match))
            for child in ast.walk(node)
        )
        long_functions.append(
            {
                "path": rel.as_posix(),
                "name": node.name,
                "start_line": int(node.lineno),
                "line_count": int(length),
                "branch_count": int(branch_count),
                "group": _group_for_path(rel),
            }
        )
    return (
        {
            "path": rel.as_posix(),
            "group": _group_for_path(rel),
            "line_count": int(lines),
            "top_level_import_count": len(imports),
            "definition_count": len(defs),
        },
        long_functions,
    )


def build_code_health_report(
    *,
    repo_root: str | Path = ".",
    include_main: bool = True,
    top_n: int = 25,
    large_file_threshold: int = 1500,
    long_function_threshold: int = 200,
) -> dict[str, Any]:
    root = Path(repo_root).expanduser().resolve()
    files: list[dict[str, Any]] = []
    functions: list[dict[str, Any]] = []
    parse_errors: list[dict[str, str]] = []
    for path in _iter_python_files(root, include_main):
        try:
            file_row, function_rows = _analyze_file(path, root)
        except Exception as exc:
            parse_errors.append({"path": path.relative_to(root).as_posix(), "error": f"{exc.__class__.__name__}: {exc}"})
            continue
        files.append(file_row)
        functions.extend(function_rows)
    group_counts: dict[str, dict[str, Any]] = {}
    for group in sorted({row["group"] for row in files}):
        group_rows = [row for row in files if row["group"] == group]
        group_counts[group] = {
            "file_count": len(group_rows),
            "line_count": sum(int(row["line_count"]) for row in group_rows),
            "large_file_count": sum(int(row["line_count"]) >= large_file_threshold for row in group_rows),
        }
    large_files = sorted(files, key=lambda row: int(row["line_count"]), reverse=True)[: max(1, int(top_n))]
    long_functions = sorted(
        [row for row in functions if int(row["line_count"]) >= int(long_function_threshold)],
        key=lambda row: (int(row["line_count"]), int(row["branch_count"])),
        reverse=True,
    )[: max(1, int(top_n))]
    largest_group = max(group_counts.items(), key=lambda item: int(item[1]["line_count"]))[0] if group_counts else None
    return {
        "code_health_report_version": CODE_HEALTH_REPORT_VERSION,
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(root),
        "file_count": len(files),
        "function_count": len(functions),
        "large_file_threshold": int(large_file_threshold),
        "long_function_threshold": int(long_function_threshold),
        "group_counts": group_counts,
        "largest_group": largest_group,
        "large_file_count": sum(int(row["line_count"]) >= int(large_file_threshold) for row in files),
        "long_function_count": sum(int(row["line_count"]) >= int(long_function_threshold) for row in functions),
        "top_large_files": large_files,
        "top_long_functions": long_functions,
        "parse_errors": parse_errors,
        "recommendations": _recommendations(group_counts, large_files, long_functions),
    }


def _recommendations(
    group_counts: dict[str, dict[str, Any]],
    large_files: list[dict[str, Any]],
    long_functions: list[dict[str, Any]],
) -> list[str]:
    recommendations: list[str] = []
    if large_files:
        first = large_files[0]
        recommendations.append(
            f"Start with the largest middle group file: {first['path']} ({first['line_count']} lines)."
        )
    group_pressure = [
        (group, int(row.get("line_count") or 0), int(row.get("large_file_count") or 0))
        for group, row in group_counts.items()
    ]
    group_pressure.sort(key=lambda item: (item[2], item[1]), reverse=True)
    if group_pressure:
        group, line_count, large_count = group_pressure[0]
        recommendations.append(
            f"Highest group pressure is {group}: {line_count} lines across {large_count} large files."
        )
    if long_functions:
        first_func = long_functions[0]
        recommendations.append(
            f"First function extraction candidate: {first_func['path']}::{first_func['name']} at line {first_func['start_line']} ({first_func['line_count']} lines)."
        )
    recommendations.append(
        "Prefer middle-group extraction over many tiny files: options, dashboard, replay, learning, and sources_news."
    )
    return recommendations


def code_health_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Code Health Report",
        "",
        f"- Version: `{report.get('code_health_report_version')}`",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Files: `{report.get('file_count')}`",
        f"- Functions: `{report.get('function_count')}`",
        f"- Large files: `{report.get('large_file_count')}`",
        f"- Long functions: `{report.get('long_function_count')}`",
        f"- Largest group: `{report.get('largest_group')}`",
        "",
        "## Recommendations",
        "",
    ]
    for item in report.get("recommendations") or []:
        lines.append(f"- {item}")
    lines.extend(["", "## Group pressure", "", "| Group | Files | Lines | Large files |", "| --- | ---: | ---: | ---: |"])
    for group, row in sorted((report.get("group_counts") or {}).items(), key=lambda item: int((item[1] or {}).get("line_count") or 0), reverse=True):
        lines.append(f"| `{group}` | {row.get('file_count')} | {row.get('line_count')} | {row.get('large_file_count')} |")
    lines.extend(["", "## Largest files", "", "| File | Group | Lines | Definitions |", "| --- | --- | ---: | ---: |"])
    for row in report.get("top_large_files") or []:
        lines.append(f"| `{row.get('path')}` | `{row.get('group')}` | {row.get('line_count')} | {row.get('definition_count')} |")
    lines.extend(["", "## Long functions", "", "| Function | Group | Lines | Branches |", "| --- | --- | ---: | ---: |"])
    for row in report.get("top_long_functions") or []:
        lines.append(f"| `{row.get('path')}::{row.get('name')}` | `{row.get('group')}` | {row.get('line_count')} | {row.get('branch_count')} |")
    if report.get("parse_errors"):
        lines.extend(["", "## Parse errors", ""])
        for row in report.get("parse_errors") or []:
            lines.append(f"- `{row.get('path')}`: {row.get('error')}")
    return "\n".join(lines)


def write_code_health_json(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def write_code_health_markdown(path: Path, report: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(code_health_markdown(report), encoding="utf-8")
    return path
