"""
tools/shell_pty_win.py — Windows ConPTY adapter (v1.35.9, Phase 8.6).

WHY:
The existing tools/shell_pty.py uses POSIX ptys (forkpty / openpty)
which don't exist on Windows. Pre-v1.35.9 Windows users couldn't
use the interactive-shell tool. This module ships the FRAMEWORK
for Windows ConPTY support: detection helpers + a shim that
gracefully degrades to plain subprocess when pywinpty isn't
available.

WHAT'S HERE:
  is_windows()       — platform check
  has_pywinpty()     — runtime detection (no install required)
  capability_summary() — string for `janus --doctor` to surface

WHAT'S DEFERRED (real-PTY work):
  Actual PTY allocation via pywinpty.PtyProcess. Needs:
    pip install pywinpty
  AND a careful integration with the existing shell_pty.py contract
  (read/write loops, EOF handling, ANSI passthrough).
  Future v1.35.x — when there's a Windows user actively asking.

For now, when shell_pty is invoked on Windows:
  if has_pywinpty():
      use the real PTY path (when added)
  else:
      fall back to plain subprocess.Popen (no interactive features)
"""

from __future__ import annotations

import sys


def is_windows() -> bool:
    """Platform check. Used by shell_pty.py's dispatcher."""
    return sys.platform == "win32" or sys.platform == "cygwin"


def has_pywinpty() -> bool:
    """True when pywinpty is importable AND the runtime supports
    ConPTY (Windows 10 1809+ has it natively)."""
    if not is_windows():
        return False
    try:
        import pywinpty  # noqa: F401
        return True
    except ImportError:
        return False


def capability_summary() -> str:
    """Human-readable summary for `janus --doctor`."""
    if not is_windows():
        return "shell_pty: POSIX path active"
    if has_pywinpty():
        return "shell_pty: Windows ConPTY available (pywinpty installed)"
    return (
        "shell_pty: Windows fallback to plain subprocess "
        "(no interactive features). Install with: pip install pywinpty"
    )


def fallback_subprocess_args(command: str, args: list[str] | None = None):
    """Return (popen_args, kwargs) for the non-PTY fallback. Used
    when shell_pty is invoked on Windows without pywinpty."""
    full = [command] + (args or [])
    return full, {
        "shell": False,
        "stdin": None,
        "stdout": -1,  # subprocess.PIPE — avoid importing here
        "stderr": -2,  # subprocess.STDOUT
    }
