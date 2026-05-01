"""
tools/edit.py — Phase 9: exact-string file edit (mirrors Claude Code Edit).

SECURITY:
- Workspace boundary via _resolve_within_workspace.
- dangerous=True; per-call approver, capability=("fs", "write", path).
- Old-string uniqueness check (unless replace_all=True) avoids
  inadvertent global rewrites.
"""

from __future__ import annotations
from typing import Callable

from . import base
from .. import config
from .fs import _resolve_within_workspace


class FsEdit(base.Tool):
    name = "fs_edit"
    description = (
        "Replace exact text in a file inside the workspace. "
        "Errors if old_string is not found, or occurs more than once "
        "and replace_all is false. DESTRUCTIVE — requires approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path relative to workspace."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default false).",
            },
        },
        "required": ["path", "old_string", "new_string"],
    }
    dangerous = True
    risk = "write"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        try:
            p = _resolve_within_workspace(args["path"])
        except ValueError as e:
            return f"error: {e}"
        if not p.exists() or not p.is_file():
            return f"error: not a file: {args['path']}"

        text = p.read_text(encoding="utf-8")
        old = args["old_string"]
        new = args["new_string"]
        replace_all = bool(args.get("replace_all"))

        count = text.count(old)
        if count == 0:
            return f"error: old_string not found in {args['path']}"
        if count > 1 and not replace_all:
            return (
                f"error: old_string occurs {count} times in {args['path']}; "
                f"set replace_all=true or pick a more specific old_string"
            )

        from .. import diff as diff_mod
        new_text_preview = (
            text.replace(old, new) if replace_all else text.replace(old, new, 1)
        )
        diff_block = diff_mod.render(text, new_text_preview, path=args["path"])
        details = (
            f"edit {p}  ({diff_mod.stat(text, new_text_preview)}, "
            f"{count} occurrence{'s' if count != 1 else ''})\n\n{diff_block}"
        )
        if not approver(
            "fs_edit",
            details,
            capability=("fs", "write", args["path"]),
        ):
            return f"refused by user: edit {args['path']}"

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        p.write_text(new_text, encoding="utf-8")
        return f"edited {args['path']} ({count} replacement{'s' if count != 1 else ''})"
