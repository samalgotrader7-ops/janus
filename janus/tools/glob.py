"""
tools/glob.py — Phase 9: workspace-bounded glob (mirrors Claude Code Glob).

Read-only. Uses Path.glob; results are validated to lie inside WORKSPACE
(belt-and-braces; Path.glob already wouldn't escape, but symlinks could
leak, so we re-check).
"""

from __future__ import annotations
from typing import Callable

from . import base
from .. import config


class FsGlob(base.Tool):
    name = "fs_glob"
    description = (
        "Find files matching a glob pattern within the workspace. "
        "Supports ** for recursive directories. Returns relative paths, "
        "one per line, capped at 200."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (e.g. 'src/**/*.py' or '*.md').",
            },
            "limit": {
                "type": "integer",
                "description": "Max results (default 200).",
            },
        },
        "required": ["pattern"],
    }
    dangerous = False

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        pattern = args.get("pattern", "").strip()
        if not pattern:
            return "error: pattern is required"
        limit = min(int(args.get("limit") or config.GLOB_MAX_RESULTS),
                    config.GLOB_MAX_RESULTS)

        results: list[str] = []
        try:
            for p in config.WORKSPACE.glob(pattern):
                # Defense-in-depth: refuse anything that escapes via symlink.
                try:
                    real = p.resolve()
                    real.relative_to(config.WORKSPACE)
                except ValueError:
                    continue
                rel = p.relative_to(config.WORKSPACE)
                results.append(str(rel).replace("\\", "/"))
                if len(results) >= limit:
                    break
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

        if not results:
            return f"(no matches for '{pattern}')"
        results.sort()
        return "\n".join(results)
