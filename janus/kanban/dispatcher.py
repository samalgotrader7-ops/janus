"""
janus.kanban.dispatcher — background polling thread.

The dispatcher is a long-lived daemon thread that:

  1. Polls the kanban store every `POLL_INTERVAL_S` seconds.
  2. Calls `store.claim_ready()` to atomically pick one READY task
     and transition it to IN_PROGRESS.
  3. Looks up the declared agent profile via `janus.agents.dispatch()`
     and runs it with the task prompt and workspace cwd.
  4. On success: transitions the task to COMPLETED, storing the
     assistant's final output. The store's set_status hook then
     advances any dependent BACKLOG tasks to READY.
  5. On failure: increments retry_count. If retries remain, sends the
     task back to BACKLOG (auto-reattempted next tick). Otherwise
     transitions to FAILED with the captured error message.

Lifecycle:

    start()  — idempotent. Creates the thread if not already running.
    stop()   — sets the cancel event. Thread exits at the top of its
               next poll iteration; the in-flight task (if any) is
               left as IN_PROGRESS and will be re-claimed on the next
               start() after a `/kanban unblock <id>` or a manual
               `set_status(id, READY)`.
    is_running() — bool.

The dispatcher runs ONE task at a time by default. Multi-worker
parallelism is achievable by start()-ing multiple dispatchers with
distinct worker_ids; the store's atomic claim handles the contention.
This module exposes one shared instance for simplicity.

Auto-start: if `config.KANBAN_AUTO_START` is True (env var
JANUS_KANBAN_DISPATCH=1), the dispatcher starts on first import. The
Telegram gateway / web gateway can opt into this for headless
deployments.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Optional

from . import store as _store
from . import state as _state


log = logging.getLogger(__name__)


POLL_INTERVAL_S = float(os.environ.get("JANUS_KANBAN_POLL_S", "2.0"))


# Module-level singleton state. Re-import-safe because Python caches
# modules — the lock and thread refs survive across slash-handler calls.
_LOCK = threading.RLock()
_THREAD: Optional[threading.Thread] = None
_STOP_EVENT: Optional[threading.Event] = None
_WORKER_ID: str = ""


def is_running() -> bool:
    with _LOCK:
        return _THREAD is not None and _THREAD.is_alive()


def start() -> bool:
    """Idempotent — returns True if a new thread was started, False
    if one was already running."""
    global _THREAD, _STOP_EVENT, _WORKER_ID
    with _LOCK:
        if is_running():
            return False
        _STOP_EVENT = threading.Event()
        _WORKER_ID = f"disp-{uuid.uuid4().hex[:8]}"
        _THREAD = threading.Thread(
            target=_run_loop,
            name="janus-kanban-dispatcher",
            args=(_STOP_EVENT, _WORKER_ID),
            daemon=True,
        )
        _THREAD.start()
        log.info("kanban dispatcher started (worker=%s)", _WORKER_ID)
        return True


def stop(timeout_s: float = 3.0) -> bool:
    """Signal the dispatcher to halt. Returns True if it was running."""
    global _THREAD, _STOP_EVENT
    with _LOCK:
        if not is_running() or _STOP_EVENT is None:
            return False
        _STOP_EVENT.set()
    # Best-effort join outside the lock so a slow tick doesn't deadlock
    # the caller (the lock is re-acquired below).
    if _THREAD is not None:
        _THREAD.join(timeout=timeout_s)
    with _LOCK:
        if _THREAD is not None and not _THREAD.is_alive():
            _THREAD = None
            _STOP_EVENT = None
            log.info("kanban dispatcher stopped")
        return True


def _run_loop(stop_event: threading.Event, worker_id: str) -> None:
    """Polling loop. Stops cleanly on stop_event."""
    while not stop_event.is_set():
        try:
            _tick(worker_id)
        except Exception:
            # Never let a tick exception kill the dispatcher.
            log.exception("kanban tick raised — continuing")
        # Sleep responsively so stop() unblocks quickly.
        stop_event.wait(POLL_INTERVAL_S)


def _tick(worker_id: str) -> None:
    """One tick: claim at most one task, execute it, transition."""
    task = _store.claim_ready(worker_id=worker_id)
    if task is None:
        return
    log.info(
        "claimed task #%s [%s] @%s: %s",
        task.id, task.status, task.agent_profile, task.title,
    )
    # Build the prompt the agent will see. If the user set a prompt
    # explicitly, use it. Otherwise synthesize one from title +
    # description so the agent has something concrete to work on.
    prompt = task.prompt.strip()
    if not prompt:
        prompt = task.title
        if task.description:
            prompt = f"{task.title}\n\n{task.description}"

    cwd = task.workspace.strip() or None

    # Run the agent. Imported here to keep cold-start cheap (the
    # agents module pulls tools, which pulls a lot).
    try:
        from .. import agents as _agents
        output = _agents.dispatch(task.agent_profile, prompt, cwd=cwd)
    except Exception as e:
        _on_failure(task, str(e))
        return

    # Some dispatch failures come back as error strings (e.g. agent
    # not found). Treat output that starts with "error:" or
    # "agent 'X' not found" as failure.
    out_lower = output.strip().lower()
    if out_lower.startswith("error:") or "not found. known agents" in out_lower:
        _on_failure(task, output[:500])
        return

    try:
        _store.set_status(task.id, _state.COMPLETED, output=output[:5000])
    except ValueError:
        # Race: someone moved this task. Log and continue — the store
        # is the source of truth, not us.
        log.warning("task #%s couldn't transition to COMPLETED", task.id)


def _on_failure(task, error: str) -> None:
    """Decide whether to retry or mark FAILED based on retry budget."""
    # Reload to get fresh retry_count (someone else might have bumped it).
    fresh = _store.get_task(task.id)
    if fresh is None:
        return
    if fresh.retry_count + 1 < fresh.max_retries:
        # Bump the counter, send back to BACKLOG. Hand-rolled UPDATE
        # because retry-count bump isn't a state transition.
        conn = _store._connect()
        with _store._txn(conn):
            conn.execute(
                "UPDATE tasks SET status = ?, retry_count = retry_count + 1, "
                "last_error = ?, worker_id = '' "
                "WHERE id = ?",
                (_state.BACKLOG, error[:500], fresh.id),
            )
        log.info(
            "task #%s failed (retry %s/%s): %s",
            fresh.id, fresh.retry_count + 1, fresh.max_retries, error[:120],
        )
        return
    try:
        _store.set_status(fresh.id, _state.FAILED, last_error=error[:500])
    except ValueError:
        log.warning("task #%s couldn't transition to FAILED", fresh.id)
