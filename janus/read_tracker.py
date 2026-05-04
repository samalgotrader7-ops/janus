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
