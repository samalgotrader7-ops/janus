"""
read_tracker.py — session-scoped fs_read tracking (v1.15.0).

WHY THIS EXISTS:
Claude Code's Edit tool refuses if you didn't Read the file in this
session. Reason: prevents the model from blind-editing a file based
on stale assumptions — particularly when the user has modified the
file between turns.

Janus pre-v1.15 had no such guard. fs_edit happily replaced text in
files the model had never read, which made certain failure modes
quiet (model assumes file shape, edit succeeds, but the result is
broken because the assumed shape doesn't match disk).

API:
  mark_read(path)  — fs_read calls this after successfully reading
                     a file. Stores the file's mtime + size at read time.
  was_read_recently(path) -> bool
                   — returns True if the file was read in this session
                     AND hasn't been modified externally since.
  reset()          — clear the tracker (per /clear or session restart).

Per-thread isolation via threading.local for the same reason as
session_context: concurrent telegram chats stay isolated.
"""

from __future__ import annotations
import threading
from pathlib import Path

_LOCAL = threading.local()


def _state() -> dict:
    if not hasattr(_LOCAL, "state"):
        _LOCAL.state = {}
    return _LOCAL.state


def mark_read(path: str | Path) -> None:
    """Record (path, mtime, size) so fs_edit can verify file unchanged."""
    try:
        p = Path(path).resolve()
        if not p.is_file():
            return
        st = p.stat()
        _state()[str(p)] = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass


def was_read_recently(path: str | Path) -> bool:
    """True iff path was marked AND mtime+size still match what we saw."""
    try:
        p = Path(path).resolve()
    except OSError:
        return False
    key = str(p)
    state = _state()
    if key not in state:
        return False
    try:
        st = p.stat()
    except OSError:
        return False
    return state[key] == (st.st_mtime_ns, st.st_size)


def reset() -> None:
    """Drop all read records for this thread."""
    if hasattr(_LOCAL, "state"):
        _LOCAL.state = {}


def all_read_paths() -> list[str]:
    """Return every path tracked. Used for tests / debugging."""
    return list(_state().keys())


# ---------- v1.25.6: read-once context awareness ----------
#
# At turn start, surface to the model the list of files it has
# already read in this session. Goal: discourage the "fs_read the
# same file 3 times in one turn" anti-pattern that the over-
# investigation (rule 22) targeted from a different angle. The
# rule says don't spelunk; this surfaces concrete evidence to back
# the rule up.

def context_summary(*, workspace: str | None = None, max_paths: int = 25) -> str:
    """Return a short Markdown block listing files this session has
    read, with sizes. Empty string when nothing has been read.

    Paths are rendered relative to ``workspace`` when one is supplied
    AND the path is inside it; otherwise the absolute path is used
    (defensive — the agent reads ~/.janus/ files too in some flows).
    """
    state = _state()
    if not state:
        return ""
    ws_path = None
    if workspace:
        try:
            ws_path = Path(workspace).resolve()
        except OSError:
            ws_path = None

    lines: list[str] = []
    for raw, (_mtime, size) in state.items():
        try:
            p = Path(raw)
        except (TypeError, ValueError):
            continue
        # Render relative when possible.
        if ws_path is not None:
            try:
                display = str(p.relative_to(ws_path))
            except ValueError:
                display = str(p)
        else:
            display = str(p)
        # Format size compactly: <1k as bytes, >=1k as kilobytes.
        if size < 1024:
            size_text = f"{size}B"
        elif size < 1024 * 1024:
            size_text = f"{size // 1024}KB"
        else:
            size_text = f"{size // (1024 * 1024)}MB"
        lines.append((display, size_text))

    if not lines:
        return ""

    # Stable order: alphabetical so the block is reproducible turn-to-turn.
    lines.sort()
    if len(lines) > max_paths:
        lines = lines[:max_paths]
        truncated = True
    else:
        truncated = False

    body = "\n".join(f"- {display} ({size})" for display, size in lines)
    if truncated:
        body += f"\n- ... ({len(state) - max_paths} more)"

    return (
        "## Files already in your session context\n"
        f"{body}\n\n"
        "(Do not fs_read these again unless you have a reason to "
        "believe they changed. Use the existing context.)"
    )
