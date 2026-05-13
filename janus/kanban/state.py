"""
janus.kanban.state — task-status states + transition rules.

State machine:

    BACKLOG ──(all parents COMPLETED)──> READY
       │                                   │
       │                                   │ (claim_task)
       │                                   v
       └──── IN_PROGRESS <─────────────────┘
                  │
                  ├──(complete_task)──> COMPLETED
                  └──(fail_task)─────> FAILED
                                          │
                                  (retry, if budget remains)
                                          │
                                          v
                                       BACKLOG

A task is READY when every entry in its parent_ids set has status
COMPLETED. The engine recomputes readiness lazily on every poll AND
eagerly when a task transitions to COMPLETED (so child tasks become
claimable within one tick).

BLOCKED is a soft pause set by the user via `/kanban block <id>` —
the dispatcher will not claim it even if dependencies are satisfied.
Unblock via `/kanban unblock <id>` (returns to READY or BACKLOG).
"""

from __future__ import annotations

# Status constants. Keep as strings — easy to inspect in the DB and
# log files; the bool/int alternative would gain nothing here.
BACKLOG = "backlog"          # waiting for parents
READY = "ready"              # parents done, eligible to be claimed
IN_PROGRESS = "in_progress"  # currently being worked
COMPLETED = "completed"      # final success
FAILED = "failed"            # final failure (after retries exhausted)
BLOCKED = "blocked"          # paused by the user

# Non-terminal — the dispatcher can still act on these.
LIVE_STATES = frozenset({BACKLOG, READY, IN_PROGRESS, BLOCKED})

# Terminal — won't move again without explicit user action.
TERMINAL_STATES = frozenset({COMPLETED, FAILED})

ALL_STATES = LIVE_STATES | TERMINAL_STATES


# Legal transitions: (from, to). The store validates against this set
# before committing — illegal transitions raise ValueError.
LEGAL_TRANSITIONS = frozenset({
    (BACKLOG, READY),
    (BACKLOG, BLOCKED),
    (READY, IN_PROGRESS),
    (READY, BLOCKED),
    (READY, BACKLOG),       # dependency re-broke (rare)
    (IN_PROGRESS, COMPLETED),
    (IN_PROGRESS, FAILED),
    (IN_PROGRESS, READY),   # claim released without completion
    (BLOCKED, BACKLOG),     # unblock — re-evaluate deps next tick
    (BLOCKED, READY),       # unblock when deps already satisfied
    (FAILED, BACKLOG),      # retry
})


def is_legal(src: str, dst: str) -> bool:
    """True if `src -> dst` is a permitted transition."""
    if src == dst:
        return True  # no-op transitions always allowed
    return (src, dst) in LEGAL_TRANSITIONS
