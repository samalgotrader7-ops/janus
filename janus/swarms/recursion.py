"""
swarms/recursion.py — thread-local depth tracking for nested swarms.

v1.4: The model cannot call swarm.run autonomously (deferred to v1.5).
But the runner already tracks depth so when the model-callable tool
lands, the recursion guard is in place.

Why thread-local rather than env var:
  janus.subagent uses os.getenv("JANUS_IS_SUBAGENT") for the plan-tree
  recursion guard because plan-tree sub-agents are SUBPROCESSES (each
  with their own env). Swarm sub-agents are THREADS sharing the parent
  process — env vars can't distinguish parent from sub-agent threads.
  threading.local() does.

Use:
  with depth_scope():           # increments on enter, decrements on exit
      run_swarm(...)
  # depth restored even on exception

  if exceeds_recursion_depth(spec.budget.max_recursion_depth):
      raise RecursionError(...)

Outside any swarm, swarm_depth() returns 0.
"""

from __future__ import annotations
import contextlib
import threading


_TLS = threading.local()


def swarm_depth() -> int:
    """Current nesting depth on this thread. 0 = not inside a swarm."""
    return getattr(_TLS, "depth", 0)


def exceeds_recursion_depth(max_depth: int) -> bool:
    """True if attempting to enter the next nested level would exceed
    max_depth. Use BEFORE incrementing (i.e., before opening a depth_scope)
    to refuse the spawn cleanly."""
    return swarm_depth() >= max_depth


@contextlib.contextmanager
def depth_scope():
    """Context manager that bumps thread-local swarm depth on enter and
    restores it on exit (even if an exception escapes the body)."""
    _TLS.depth = swarm_depth() + 1
    try:
        yield _TLS.depth
    finally:
        _TLS.depth = max(0, swarm_depth() - 1)


def reset_for_thread() -> None:
    """Force depth=0 on the current thread. Used by tests; not normally
    needed since depth_scope is exception-safe."""
    if hasattr(_TLS, "depth"):
        try:
            del _TLS.depth
        except AttributeError:
            pass
