"""
security.py — public API for Janus's capability tokens + workspace boundary.

WHY:
The CLI_ENHANCEMENT_PLAN §3 calls out "Capability sandbox for ANY local
agent" — exposing our `Capability` + workspace resolver as a small
library other tools can adopt. This module is the entry point: a stable
import surface that other projects can pin to without depending on the
rest of Janus.

USAGE FROM ANOTHER AGENT FRAMEWORK:

    from janus.security import (
        Capability, CapabilitySet, resolve_within,
    )

    caps = CapabilitySet.from_dict({
        "shell.exec": ["git *", "pnpm *"],
        "fs.write":   ["src/**"],
    })
    if caps.grants("shell", "exec", "git status"):
        ...

    safe_path = resolve_within(workspace, user_provided_path)

P-INVARIANTS:
- The workspace check is geometric (`Path.resolve()` + `relative_to`),
  not a regex. Symlinks, `..`, and absolute paths outside workspace are
  all blocked uniformly. (P3.)
- Capability matching is glob-only (no regex). Less expressive on
  purpose — easier to read and audit. (P2.)
"""

from __future__ import annotations
from pathlib import Path
from typing import Union

from .tools.capabilities import Capability, CapabilitySet


def resolve_within(workspace: Union[str, Path], rel_path: str) -> Path:
    """Resolve `rel_path` relative to `workspace`. Refuses any resolved
    path that escapes the workspace.

    Raises ValueError on escape — callers MUST treat that as the user's
    request being refused, not as a bug.
    """
    ws = Path(workspace).resolve()
    candidate = (ws / rel_path).resolve()
    try:
        candidate.relative_to(ws)
    except ValueError:
        raise ValueError(
            f"path '{rel_path}' resolves outside workspace {ws}"
        )
    return candidate


__all__ = ["Capability", "CapabilitySet", "resolve_within"]
