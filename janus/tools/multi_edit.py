"""
tools/multi_edit.py — Phase 9: atomic multi-file edit (mirrors Claude Code MultiEdit).

ATOMICITY:
- Pre-flight: every edit's old_string must be present (and unique unless
  replace_all=True). If any pre-flight fails, NO file is touched.
- Approval: approver is called once per distinct path. Refusal at any path
  aborts before any write.
- Commit: writes happen sequentially; if any write raises, prior writes are
  rolled back to their original contents.

This means "all-or-nothing" in three layers — validation, approval, and
commit. The build guide acceptance criterion ("atomic multi-file") asks
for this end-to-end, not just one of the three.
"""

from __future__ import annotations
from pathlib import Path
from typing import Callable

from . import base
from .fs import _resolve_within_workspace


class FsMultiEdit(base.Tool):
    name = "fs_multi_edit"
    description = (
        "Apply multiple find/replace edits across one or more files atomically. "
        "All-or-nothing: if any edit cannot apply (string missing, ambiguous, "
        "or refused by approval) NO file is changed. DESTRUCTIVE — each "
        "distinct path requires approval."
    )
    parameters = {
        "type": "object",
        "properties": {
            "edits": {
                "type": "array",
                "description": "List of edits. Each: {path, old_string, new_string, replace_all?}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["path", "old_string", "new_string"],
                },
            }
        },
        "required": ["edits"],
    }
    dangerous = True

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        edits = args.get("edits") or []
        if not isinstance(edits, list) or not edits:
            return "error: edits must be a non-empty array"

        # Pre-flight: validate paths and apply each edit in memory.
        staged: dict[Path, str] = {}        # path -> current staged text
        originals: dict[Path, str] = {}     # path -> original text (rollback)
        rels: dict[Path, str] = {}          # path -> relative path string for approver
        for i, edit in enumerate(edits):
            rel = str(edit.get("path") or "")
            if not rel:
                return f"error: edit #{i}: missing path"
            try:
                p = _resolve_within_workspace(rel)
            except ValueError as e:
                return f"error: edit #{i}: {e}"
            if not p.exists() or not p.is_file():
                return f"error: edit #{i}: not a file: {rel}"

            if p not in originals:
                originals[p] = p.read_text(encoding="utf-8")
                staged[p] = originals[p]
                rels[p] = rel

            text = staged[p]
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            replace_all = bool(edit.get("replace_all"))

            count = text.count(old)
            if count == 0:
                return f"error: edit #{i}: old_string not found in {rel}"
            if count > 1 and not replace_all:
                return (
                    f"error: edit #{i}: old_string occurs {count} times in "
                    f"{rel}; set replace_all=true or pick a more specific old_string"
                )
            staged[p] = (
                text.replace(old, new)
                if replace_all
                else text.replace(old, new, 1)
            )

        # Approval per distinct path. Skip files whose staged text is unchanged.
        changed = [p for p, t in staged.items() if t != originals[p]]
        if not changed:
            return "error: no edit produced any change"

        from .. import diff as diff_mod
        for p in sorted(changed, key=lambda x: rels[x]):
            d = diff_mod.render(originals[p], staged[p], path=rels[p])
            details = (
                f"multi-edit: {rels[p]}  ({diff_mod.stat(originals[p], staged[p])}, "
                f"{len(changed)} files in this batch)\n\n{d}"
            )
            if not approver(
                "fs_multi_edit",
                details,
                capability=("fs", "write", rels[p]),
            ):
                return f"refused by user: edit {rels[p]} (no files written)"

        # Commit, with rollback on partial failure.
        written: list[Path] = []
        for p in changed:
            try:
                p.write_text(staged[p], encoding="utf-8")
                written.append(p)
            except Exception as e:
                # Roll back any prior writes to their original contents.
                for wp in written:
                    try:
                        wp.write_text(originals[wp], encoding="utf-8")
                    except Exception:
                        pass
                return (
                    f"error during commit at {rels[p]}: {type(e).__name__}: {e} "
                    f"(rolled back {len(written)} prior write(s))"
                )

        return f"applied {len(edits)} edit(s) across {len(changed)} file(s)"
