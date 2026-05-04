"""
tools/fs.py — filesystem tool, workspace-bounded.

SECURITY POSTURE:
All paths resolve against config.WORKSPACE. Any resolved path that escapes
the workspace is refused. This is the architectural defense — not a regex
blacklist (which Hermes uses and which is bypassable). Path traversal,
symlink escapes, absolute paths outside workspace: all blocked by the same
single check.

The 'write' action is dangerous=True at the operation level. It gates per
call through the approver.
"""

from __future__ import annotations
from pathlib import Path
from typing import Callable

from . import base
from .. import config

MAX_READ_BYTES = 200_000
MAX_LIST_ENTRIES = 500


def _resolve_within_workspace(rel_path: str) -> Path:
    """Resolve a path and ensure it's inside WORKSPACE. Raises ValueError if not.

    Phase 19: thin wrapper around `security.resolve_within` so the public
    library and the in-tree tools share one implementation.
    """
    from .. import security
    return security.resolve_within(config.WORKSPACE, rel_path)


class FsRead(base.Tool):
    name = "fs_read"
    description = (
        "Read the contents of a text file inside the workspace. "
        "Returns up to 200KB. Use for source code, config, docs, data files."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to workspace (e.g. 'src/main.py').",
            }
        },
        "required": ["path"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[[str, str], bool]) -> str:
        p = _resolve_within_workspace(args["path"])
        if not p.exists():
            return f"error: file not found: {args['path']}"
        if not p.is_file():
            return f"error: not a file: {args['path']}"
        data = p.read_bytes()[:MAX_READ_BYTES]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return f"error: file is not utf-8 text: {args['path']}"
        # v1.15.0 — record this read so fs_edit can verify the file
        # didn't change before editing (Claude Code Edit safety pattern).
        try:
            from .. import read_tracker
            read_tracker.mark_read(p)
        except Exception:
            pass
        return text


class FsWrite(base.Tool):
    name = "fs_write"
    description = (
        "Write text content to a file inside the workspace. Creates or overwrites. "
        "DESTRUCTIVE — every call requires user approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to workspace."},
            "content": {"type": "string", "description": "Full file contents."},
        },
        "required": ["path", "content"],
    }
    dangerous = True
    risk = "write"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        from .. import diff as diff_mod
        p = _resolve_within_workspace(args["path"])
        action = "overwrite" if p.exists() else "create"
        size = len(args["content"])
        if action == "overwrite":
            try:
                old = p.read_text(encoding="utf-8")
            except Exception:
                old = ""
            d = diff_mod.render(old, args["content"], path=args["path"])
            details = (
                f"overwrite {p}  ({diff_mod.stat(old, args['content'])}, "
                f"{size} bytes)\n\n{d}"
                if d else
                f"overwrite {p}  ({size} bytes, no textual change)"
            )
        else:
            preview = args["content"][:600]
            details = (
                f"create {p}  ({size} bytes)\n\n"
                f"--- proposed contents ---\n{preview}"
                + ("\n... [truncated]" if size > 600 else "")
            )
        if not approver(
            f"fs_write: {action}",
            details,
            capability=("fs", "write", args["path"]),
        ):
            return f"refused by user: write to {args['path']}"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"], encoding="utf-8")
        return f"wrote {size} bytes to {args['path']}"


class FsList(base.Tool):
    name = "fs_list"
    description = (
        "List files and directories at a path inside the workspace. "
        "Returns up to 500 entries, one per line, with type marker (f/d)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Path relative to workspace. Use '.' for workspace root.",
            }
        },
        "required": ["path"],
    }
    dangerous = False
    risk = "read"

    def run(self, args: dict, approver: Callable[[str, str], bool]) -> str:
        p = _resolve_within_workspace(args["path"])
        if not p.exists():
            return f"error: not found: {args['path']}"
        if not p.is_dir():
            return f"error: not a directory: {args['path']}"
        lines = []
        for child in sorted(p.iterdir())[:MAX_LIST_ENTRIES]:
            kind = "d" if child.is_dir() else "f"
            lines.append(f"{kind} {child.name}")
        if not lines:
            return "(empty directory)"
        return "\n".join(lines)
