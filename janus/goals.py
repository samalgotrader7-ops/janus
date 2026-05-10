"""
goals.py — /goal Ralph Loop state primitive (v1.37.0, Phase 10.1.0).

WHY:
A standing objective that survives turns. Sam wants Janus to be a
true autonomous worker: set a goal, let it run, judge model decides
when it's achieved. This module is the storage primitive — one JSON
file per scope on disk so /goal status survives `Ctrl-C` + `janus`
relaunch and so the future telegram + web parity (v1.37.2) reads
the same state without IPC.

v10.1.0 ships ONLY the state primitive + slash commands. The
auto-continue loop + judge model lands in v10.1.1.

STORAGE:
~/.janus/goals/<scope>.json — one file per scope. The "scope" is
whatever the surface declares as its session identity:
  cli_rich:   "cli_rich"
  telegram:   "telegram:<chat_id>"
  web:        "web:<session_id>"
For v10.1.0 only cli_rich is wired; the schema is shape-compatible
with the future surfaces.

GOAL STATE FIELDS:
  text          — the user's stated objective
  status        — 'active' | 'paused' | 'done' | 'cleared'
  turn_budget   — max turns the loop is allowed (default 500)
  turns_used    — turns counted toward the budget
  created_at    — unix timestamp when set
  updated_at    — unix timestamp of last status change
  paused_at     — unix timestamp when paused (None if not paused)

DESIGN DECISIONS (locked 2026-05-10 with Sam):
  * Default turn budget: 500
  * Cycle-detection auto-pause: ON (lands in v10.1.1)
  * Plan-mode auto-leave: ON (lands in v10.1.1)
  * Judge model: cheap (haiku/gpt-4o-mini) (lands in v10.1.1)

NO LOCKING:
Single-machine assumption (per Sam) — no fcntl flock. Atomic write
via temp-file rename keeps a partial write from corrupting state.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from . import config


DEFAULT_TURN_BUDGET = 500

VALID_STATUSES = ("active", "paused", "done", "cleared")


@dataclass
class GoalState:
    """One standing objective. Persisted to ~/.janus/goals/<scope>.json."""
    text: str
    status: str = "active"
    turn_budget: int = DEFAULT_TURN_BUDGET
    turns_used: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    paused_at: Optional[float] = None

    def is_active(self) -> bool:
        return self.status == "active"

    def remaining_turns(self) -> int:
        return max(0, self.turn_budget - self.turns_used)

    def budget_exhausted(self) -> bool:
        return self.turns_used >= self.turn_budget


# ---------- storage ----------


def _goals_dir() -> Path:
    """Where per-scope goal files live. Created on demand."""
    d = config.HOME / "goals"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scope_path(scope: str) -> Path:
    """Path for a given scope. Scope must be a non-empty filename-safe
    string. ':' (used by telegram:<chat_id> / web:<session>) is
    replaced with '_' since some filesystems reject it."""
    if not scope or not scope.strip():
        raise ValueError("scope required")
    safe = scope.strip().replace(":", "_").replace("/", "_").replace("\\", "_")
    return _goals_dir() / f"{safe}.json"


def load(scope: str) -> Optional[GoalState]:
    """Load the goal for `scope`. Returns None if no goal is set."""
    p = _scope_path(scope)
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(raw, dict) or "text" not in raw:
        return None
    return GoalState(
        text=str(raw.get("text", "")),
        status=str(raw.get("status", "active")),
        turn_budget=int(raw.get("turn_budget", DEFAULT_TURN_BUDGET)),
        turns_used=int(raw.get("turns_used", 0)),
        created_at=float(raw.get("created_at", time.time())),
        updated_at=float(raw.get("updated_at", time.time())),
        paused_at=raw.get("paused_at"),
    )


def save(scope: str, goal: GoalState) -> None:
    """Atomic write: temp file + rename. Updates `updated_at` first."""
    goal.updated_at = time.time()
    p = _scope_path(scope)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(asdict(goal), indent=2), encoding="utf-8")
    os.replace(tmp, p)


def clear(scope: str) -> bool:
    """Delete the goal file for `scope`. Returns True if removed."""
    p = _scope_path(scope)
    if p.is_file():
        try:
            p.unlink()
            return True
        except OSError:
            return False
    return False


# ---------- state transitions ----------


def set_goal(scope: str, text: str, *, turn_budget: int = DEFAULT_TURN_BUDGET) -> GoalState:
    """Replace any existing goal with a fresh active one."""
    if not text or not text.strip():
        raise ValueError("goal text required")
    if turn_budget <= 0:
        raise ValueError("turn_budget must be positive")
    goal = GoalState(
        text=text.strip(),
        status="active",
        turn_budget=int(turn_budget),
        turns_used=0,
    )
    save(scope, goal)
    return goal


def pause(scope: str) -> Optional[GoalState]:
    """Mark the goal paused. Returns updated state, or None if no goal."""
    g = load(scope)
    if g is None:
        return None
    if g.status != "active":
        return g  # idempotent — paused/done/cleared stays as is
    g.status = "paused"
    g.paused_at = time.time()
    save(scope, g)
    return g


def resume(scope: str) -> Optional[GoalState]:
    """Mark a paused goal active again."""
    g = load(scope)
    if g is None:
        return None
    if g.status != "paused":
        return g
    g.status = "active"
    g.paused_at = None
    save(scope, g)
    return g


def mark_done(scope: str) -> Optional[GoalState]:
    """Judge model has decided the goal is achieved. Wired in v10.1.1."""
    g = load(scope)
    if g is None:
        return None
    g.status = "done"
    save(scope, g)
    return g


def increment_turns(scope: str, n: int = 1) -> Optional[GoalState]:
    """Bump turn counter — called by the auto-continue loop in v10.1.1."""
    g = load(scope)
    if g is None or g.status != "active":
        return g
    g.turns_used += n
    save(scope, g)
    return g


# ---------- formatting ----------


def format_status(g: Optional[GoalState]) -> str:
    """Single-line/multiline string suitable for slash output."""
    if g is None:
        return "no goal set. Use `/goal <text>` to set one."
    age = max(0, int(time.time() - g.created_at))
    age_s = _human_duration(age)
    lines = [
        f"goal: {g.text}",
        f"status: {g.status}    "
        f"turns: {g.turns_used}/{g.turn_budget} "
        f"({g.remaining_turns()} left)    "
        f"set {age_s} ago",
    ]
    if g.status == "paused" and g.paused_at:
        lines.append(f"paused: {_human_duration(int(time.time() - g.paused_at))} ago")
    return "\n".join(lines)


def _human_duration(secs: int) -> str:
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"
