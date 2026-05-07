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


def _resolve_for_read(rel_path: str) -> Path:
    """Resolve for READ-ONLY tools (fs_read, fs_list).

    Tries WORKSPACE first (the user's project — primary boundary). If
    that fails, ALSO accepts paths inside ``~/.janus/`` (the agent's own
    state dir — memory cards, skills, triggers, conversations, log).
    Without this carve-out, an agent running with WORKSPACE=/opt/janus
    can't fs_read its OWN ~/.janus/memory/user.md and falls back to
    `shell cat ...` — which needs per-call approval and is the v1.18.1
    Telegram pain point Sam hit on 2026-05-06.

    P3 (workspace boundary) is preserved for WRITE tools — fs_write /
    fs_edit / fs_multi_edit still go through ``_resolve_within_workspace``.
    Only reads see the agent's home dir as a second valid root.

    ORDER MATTERS: tilde-prefixed paths must be expanded BEFORE the
    workspace check, because Path("/ws/") / "~/foo" wrongly produces
    "/ws/~/foo" (which IS literally under workspace) — the workspace
    resolver would happily return that bogus path and fs_read would
    then fail with "not found".
    """
    from .. import security
    # FIRST: tilde-prefixed paths expand against the agent's home.
    # (Must come before workspace check — see docstring for why.)
    if rel_path.startswith("~"):
        expanded = Path(rel_path).expanduser().resolve()
        try:
            expanded.relative_to(Path(config.HOME).resolve())
            return expanded
        except ValueError:
            pass
    # SECOND: workspace (the normal case for code paths).
    try:
        return security.resolve_within(config.WORKSPACE, rel_path)
    except ValueError:
        pass
    # THIRD: the agent's own ~/.janus/ tree (read-only carve-out for
    # absolute paths like /home/sam/.janus/memory/user.md).
    try:
        return security.resolve_within(config.HOME, rel_path)
    except ValueError:
        pass
    raise ValueError(
        f"path '{rel_path}' resolves outside both workspace "
        f"({config.WORKSPACE}) and agent home ({config.HOME})"
    )


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
        # v1.18.2: read-only tools accept paths under ~/.janus/ in
        # addition to WORKSPACE so the agent can introspect its own
        # state dir without falling back to `shell cat`.
        try:
            p = _resolve_for_read(args["path"])
        except ValueError as e:
            return f"error: {e}"
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
        # v1.25.4: pass structured diff data so cli_rich's approver can
        # render with Rich Syntax (line numbers + diff highlighting)
        # instead of the ANSI text dump. Surfaces that ignore the
        # kwarg (basic CLI, web, telegram) keep getting the rendered
        # details string — backward compatible.
        diff_data = (
            {"old": old, "new": args["content"], "path": args["path"]}
            if action == "overwrite" else None
        )
        if not approver(
            f"fs_write: {action}",
            details,
            capability=("fs", "write", args["path"]),
            diff_data=diff_data,
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
        # v1.18.2: same carve-out as FsRead — agent can list its own
        # ~/.janus/ state dir without shell-fallback.
        try:
            p = _resolve_for_read(args["path"])
        except ValueError as e:
            return f"error: {e}"
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
