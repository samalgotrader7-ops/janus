"""
tools/grep.py — Phase 9: content search (mirrors Claude Code Grep).

Uses ripgrep (`rg`) when on PATH for speed; falls back to a Python regex
walk over the workspace if not. Workspace boundary: search root resolves
through _resolve_within_workspace.
"""

from __future__ import annotations
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from . import base
from .. import config
from .fs import _resolve_within_workspace


class FsGrep(base.Tool):
    name = "fs_grep"
    description = (
        "Search file contents for a regex inside the workspace. Uses "
        "ripgrep when available (fast); falls back to a Python regex "
        "scan otherwise. Returns up to 200 matching lines."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern."},
            "path": {
                "type": "string",
                "description": "Subdirectory of workspace to search (default '.').",
            },
            "glob": {
                "type": "string",
                "description": "Optional file glob filter, e.g. '*.py'.",
            },
            "limit": {"type": "integer", "description": "Max matching lines (default 200)."},
            "case_insensitive": {"type": "boolean"},
        },
        "required": ["pattern"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        pattern = args.get("pattern", "")
        if not pattern:
            return "error: pattern is required"
        path_arg = args.get("path") or "."
        glob_filter = args.get("glob")
        limit = min(int(args.get("limit") or config.GREP_MAX_LINES),
                    config.GREP_MAX_LINES)
        case_i = bool(args.get("case_insensitive"))

        try:
            search_root = _resolve_within_workspace(path_arg)
        except ValueError as e:
            return f"error: {e}"
        if not search_root.exists():
            return f"error: not found: {path_arg}"

        rg = shutil.which("rg")
        if rg:
            return _rg(rg, pattern, search_root, glob_filter, limit, case_i)
        return _py_grep(pattern, search_root, glob_filter, limit, case_i)


def _rg(
    rg: str, pattern: str, root: Path,
    glob_filter: str | None, limit: int, case_i: bool,
) -> str:
    cmd = [rg, "--no-heading", "-n", "--max-count", str(limit)]
    if case_i:
        cmd.append("-i")
    if glob_filter:
        cmd.extend(["--glob", glob_filter])
    cmd.extend(["--", pattern, str(root)])
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=config.GREP_TIMEOUT, errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"error: rg timed out after {config.GREP_TIMEOUT}s"
    out = proc.stdout or ""
    if not out.strip():
        return "(no matches)"
    lines = out.splitlines()[:limit]
    return "\n".join(lines)


def _py_grep(
    pattern: str, root: Path,
    glob_filter: str | None, limit: int, case_i: bool,
) -> str:
    flags = re.IGNORECASE if case_i else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return f"error: invalid regex: {e}"

    files = (
        list(root.rglob(glob_filter))
        if glob_filter
        else [p for p in root.rglob("*") if p.is_file()]
    )
    matches: list[str] = []
    for f in files:
        if not f.is_file():
            continue
        try:
            for n, line in enumerate(
                f.read_text(encoding="utf-8", errors="replace").splitlines(),
                start=1,
            ):
                if rx.search(line):
                    rel = f.relative_to(config.WORKSPACE)
                    matches.append(
                        f"{str(rel).replace(chr(92), '/')}:{n}:{line[:200]}"
                    )
                    if len(matches) >= limit:
                        break
        except (OSError, UnicodeDecodeError):
            continue
        if len(matches) >= limit:
            break
    if not matches:
        return "(no matches)"
    return "\n".join(matches)
