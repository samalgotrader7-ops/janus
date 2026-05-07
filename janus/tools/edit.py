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

        # v1.15.0 — refuse if the file wasn't read in this session, OR
        # if it was modified externally since the last read. Same pattern
        # Claude Code uses to prevent blind edits based on stale
        # assumptions. Override via JANUS_FS_EDIT_REQUIRE_READ=0.
        import os as _os
        if _os.getenv("JANUS_FS_EDIT_REQUIRE_READ", "1") not in ("0", "false"):
            try:
                from .. import read_tracker
                if not read_tracker.was_read_recently(p):
                    # Distinguish "never read" from "modified since".
                    if str(p.resolve()) in read_tracker.all_read_paths():
                        return (
                            f"error: {args['path']} was modified since you "
                            f"last read it. Re-read with fs_read first, "
                            f"then edit."
                        )
                    return (
                        f"error: must fs_read({args['path']!r}) before "
                        f"fs_edit. Reading first prevents blind edits "
                        f"based on stale assumptions about file shape."
                    )
            except Exception:
                pass  # If the tracker fails, don't block the edit.

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
        # v1.25.4: pass structured diff so cli_rich approver can render
        # with Rich Syntax. Other surfaces ignore this kwarg.
        if not approver(
            "fs_edit",
            details,
            capability=("fs", "write", args["path"]),
            diff_data={
                "old": text, "new": new_text_preview, "path": args["path"],
            },
        ):
            return f"refused by user: edit {args['path']}"

        new_text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        p.write_text(new_text, encoding="utf-8")
        # Re-mark as read so subsequent fs_edits in the same turn don't
        # spuriously fail the "modified since read" check (the
        # modification was OUR edit).
        try:
            from .. import read_tracker
            read_tracker.mark_read(p)
        except Exception:
            pass
        return f"edited {args['path']} ({count} replacement{'s' if count != 1 else ''})"
