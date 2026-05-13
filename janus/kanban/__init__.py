"""
janus.kanban — durable, SQLite-backed task board for multi-agent
coordination (v1.42.0).

WHY:
Single-agent loops are bottlenecked on the one model's ability to hold
state and pick the next step. A kanban board flips that around: the
BOARD is the state, agents are workers that claim work, do it, hand
off, and disappear. Multiple specialised agents (`developer`,
`researcher`, `coder`, `documenter`, `reviewer`, `tester`) can
co-operate on a single goal in parallel, blocked only by real
data-dependencies between tasks.

ARCHITECTURE:
  store        — SQLite schema + CRUD + atomic state transitions
  state        — task-status enum + transition rules + dependency engine
  dispatcher   — polling thread that claims READY tasks, spawns the
                 declared agent, captures output, advances dependents
  (tools/kanban.py) — model-callable tools for the agents themselves
                 to add / list / claim tasks during their turn
  (slash_dispatch.py) — `/kanban` slash commands for the user

DB lives at ~/.janus/kanban.db; settings drive the dispatcher
(JANUS_KANBAN_DISPATCH=1 to auto-start).
"""

from . import state  # noqa: F401  (re-export for convenience)
from . import store  # noqa: F401

__all__ = ["state", "store"]
