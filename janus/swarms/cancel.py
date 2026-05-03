"""
swarms/cancel.py — cancellation token for in-flight swarms.

Two-tier model:
  1. In-process: a threading.Event the runner passes into every sub-agent
     dispatch. The executor checks `event.is_set()` between steps and
     returns early when set.
  2. Cross-process: a `cancel.flag` file in the swarm's run dir, written
     by `janus swarm cancel <run-id>` from any process. A background
     watcher thread in the coordinator polls the flag and mirrors its
     state into the in-process event so currently-running sub-agents
     see the cancellation between steps (rather than only at the next
     dispatch boundary).

Cancellation is COOPERATIVE. Sub-agents finish their current step
(usually one LLM call + any in-flight tool calls), then exit cleanly.
We don't kill threads — Python's threading model doesn't support clean
forced cancellation. Bound the per-step damage with MAX_STEPS and
LLM_TIMEOUT.
"""

from __future__ import annotations
import threading

from . import state


class CancellationToken:
    """Cancellation primitive for one swarm run.

    Construct in the coordinator before phase 0. Call `start_watcher()` to
    begin polling the cancel.flag file in the background. Pass `event`
    into each sub-agent dispatch (executor.execute checks it between
    steps). Call `stop_watcher()` in a finally block at the end of the
    swarm.
    """

    def __init__(self, run_id: str, *, poll_interval_s: float = 0.5):
        self.run_id = run_id
        self.poll_interval_s = poll_interval_s
        self.event = threading.Event()
        self._stop_flag = threading.Event()
        self._watcher: threading.Thread | None = None

    def is_cancelled(self) -> bool:
        """Truth source: in-process event OR on-disk flag.
        Side effect: if the on-disk flag exists, mirrors into the event."""
        if self.event.is_set():
            return True
        if state.is_cancelled(self.run_id):
            self.event.set()
            return True
        return False

    def cancel(self) -> None:
        """Programmatic cancel — sets the in-process event AND writes the
        on-disk flag for cross-process visibility."""
        self.event.set()
        state.write_cancel_flag(self.run_id)

    def start_watcher(self) -> None:
        """Begin background polling of cancel.flag. Idempotent."""
        if self._watcher is not None and self._watcher.is_alive():
            return

        def _watch() -> None:
            while not self._stop_flag.is_set():
                if state.is_cancelled(self.run_id):
                    self.event.set()
                    return
                # Wait yields to other threads cleanly; cancellable.
                if self._stop_flag.wait(self.poll_interval_s):
                    return

        self._watcher = threading.Thread(
            target=_watch, name=f"swarm-cancel-watcher-{self.run_id}",
            daemon=True,
        )
        self._watcher.start()

    def stop_watcher(self, *, timeout: float = 1.0) -> None:
        """Signal the watcher to exit and join it. Idempotent."""
        self._stop_flag.set()
        if self._watcher is not None and self._watcher.is_alive():
            self._watcher.join(timeout=timeout)
