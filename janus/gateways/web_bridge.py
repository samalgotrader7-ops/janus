"""
gateways/web_bridge.py — v1.22.0a async approval/clarify bridge.

PROBLEM:
The web gateway calls executor.chat() which calls approver(...)
synchronously. Pre-v1.22.0a the web approver returned False on every
ASK decision because there was no UI to ask through. Tools requiring
approval just got denied.

DESIGN:
1. /chat runs executor.chat() in a thread via asyncio.to_thread so
   approver's blocking wait doesn't freeze the FastAPI event loop.
2. When approver(...) is invoked from the thread:
   * It generates a request_id and creates a threading.Event.
   * It schedules an SSE notification on the running event loop using
     asyncio.run_coroutine_threadsafe.
   * It blocks on the threading.Event with a 30-min timeout.
3. The browser is subscribed to /api/events (Server-Sent Events). On
   receiving an `approval_pending` event it shows a modal.
4. User clicks approve/deny → POST /api/approve/{request_id} sets the
   threading.Event and stashes the decision.
5. The thread wakes up, reads the decision, returns to executor.

Same shape for clarify (text answer + optional choices).

CROSS-THREAD COORDINATION:
- _state_lock guards _pending_* dicts and _event_subscribers.
- threading.Event is the synchronization primitive (not asyncio.Event,
  because the approver runs in a worker thread, not the event loop).
- run_coroutine_threadsafe is the correct way for thread → loop
  notification.

TIMEOUTS:
30 minutes default — same as Telegram approval (matches
JANUS_TELEGRAM_APPROVAL_TIMEOUT). Configurable via
JANUS_WEB_APPROVAL_TIMEOUT.

PRIVACY:
Each subscriber is keyed by auth_sid. An approval request raised by a
user's chat session is broadcast ONLY to subscribers for that same
auth_sid. Other authenticated users (in a future multi-user setup)
never see each other's approvals.
"""
from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from typing import Any


# ---------- module-level state ----------

_state_lock = threading.Lock()

# request_id -> {
#   "event": threading.Event, "decision": bool,
#   "label": str, "details": str, "risk": str,
#   "auth_sid": str, "ts": float,
# }
_pending_approvals: dict[str, dict] = {}

# request_id -> {
#   "event": threading.Event, "answer": str | None,
#   "question": str, "choices": list[str],
#   "auth_sid": str, "ts": float,
# }
_pending_clarifies: dict[str, dict] = {}

# auth_sid -> list[asyncio.Queue]. Each browser SSE connection adds
# itself; broadcasts iterate this list.
_event_subscribers: dict[str, list[asyncio.Queue]] = {}


def _approval_timeout() -> float:
    return float(os.environ.get("JANUS_WEB_APPROVAL_TIMEOUT", "1800"))


def _clarify_timeout() -> float:
    return float(os.environ.get("JANUS_WEB_CLARIFY_TIMEOUT", "1800"))


# ---------- subscriber management (called from FastAPI route) ----------


def add_subscriber(auth_sid: str) -> asyncio.Queue:
    """Called from the SSE route. Returns a queue the caller awaits on."""
    q: asyncio.Queue = asyncio.Queue()
    with _state_lock:
        _event_subscribers.setdefault(auth_sid, []).append(q)
    return q


def remove_subscriber(auth_sid: str, q: asyncio.Queue) -> None:
    """Called when the SSE generator exits (browser closed)."""
    with _state_lock:
        subs = _event_subscribers.get(auth_sid)
        if subs and q in subs:
            subs.remove(q)
        if subs is not None and not subs:
            _event_subscribers.pop(auth_sid, None)


def subscriber_count(auth_sid: str = "") -> int:
    """Test/diagnostic helper. Empty arg returns total across all sids."""
    with _state_lock:
        if auth_sid:
            return len(_event_subscribers.get(auth_sid, []))
        return sum(len(v) for v in _event_subscribers.values())


# ---------- broadcasting (called from the worker thread + helpers) ----------


def _broadcast_sync(auth_sid: str, event: dict) -> None:
    """Push an event to every subscriber for auth_sid.

    Called from the event loop. Each queue.put_nowait is non-blocking;
    if a subscriber's queue is somehow full (shouldn't happen with
    unbounded asyncio.Queue), we silently drop.
    """
    with _state_lock:
        subs = list(_event_subscribers.get(auth_sid, []))
    for q in subs:
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass


def _broadcast_from_thread(loop: asyncio.AbstractEventLoop,
                           auth_sid: str, event: dict) -> None:
    """Schedule a broadcast from a worker thread onto the loop."""
    if loop is None:
        return
    asyncio.run_coroutine_threadsafe(
        _broadcast_async(auth_sid, event), loop,
    )


async def _broadcast_async(auth_sid: str, event: dict) -> None:
    _broadcast_sync(auth_sid, event)


# ---------- approval request (called from the worker thread) ----------


def request_approval(
    auth_sid: str,
    loop: asyncio.AbstractEventLoop,
    label: str,
    details: str,
    risk: str,
    plan: dict | None = None,
) -> bool:
    """Block until the user decides. Called from the chat worker thread.

    Returns the user's decision (True=approve, False=deny). Returns
    False on timeout.

    v1.30.0 — when ``plan`` is set (for ExitPlanMode actions), the
    structured payload is included on the ``approval_pending`` SSE
    event and on the ``list_pending_approvals`` bootstrap. The web
    client uses its presence to switch from the generic approval modal
    to the dedicated plan-review modal.
    """
    request_id = uuid.uuid4().hex
    ev = threading.Event()
    with _state_lock:
        _pending_approvals[request_id] = {
            "event": ev,
            "decision": False,
            "label": label,
            "details": details,
            "risk": risk,
            "auth_sid": auth_sid,
            "ts": time.time(),
            "plan": plan,
        }
    event = {
        "type": "approval_pending",
        "request_id": request_id,
        "label": label,
        "details": details,
        "risk": risk,
    }
    if plan is not None:
        event["plan"] = plan
    _broadcast_from_thread(loop, auth_sid, event)
    timed_out = not ev.wait(timeout=_approval_timeout())
    with _state_lock:
        entry = _pending_approvals.pop(request_id, None)
    decision = bool(entry["decision"]) if entry and not timed_out else False
    # Tell other subscribers (other tabs) to dismiss their modal.
    _broadcast_from_thread(loop, auth_sid, {
        "type": "approval_resolved",
        "request_id": request_id,
        "decision": decision,
        "timed_out": timed_out,
    })
    return decision


def resolve_approval(request_id: str, decision: bool) -> bool:
    """Called from the FastAPI POST /api/approve/{id} handler.

    Returns True if the request was found and resolved, False if it's
    expired or unknown.
    """
    with _state_lock:
        entry = _pending_approvals.get(request_id)
        if entry is None:
            return False
        entry["decision"] = bool(decision)
        entry["event"].set()
        return True


def list_pending_approvals(auth_sid: str = "") -> list[dict]:
    """Diagnostic / refresh-after-reconnect helper.

    The browser may reconnect mid-flight; on connect it can call this
    via /api/events bootstrap to hydrate any modals that fired before
    the SSE connection opened.
    """
    with _state_lock:
        out: list[dict] = []
        for rid, e in _pending_approvals.items():
            if auth_sid and e["auth_sid"] != auth_sid:
                continue
            entry = {
                "request_id": rid,
                "label": e["label"],
                "details": e["details"],
                "risk": e["risk"],
                "ts": e["ts"],
            }
            # v1.30.0 — include the plan payload on bootstrap so a tab
            # that reconnects mid-flight gets the dedicated plan modal,
            # not the generic one.
            plan = e.get("plan")
            if plan is not None:
                entry["plan"] = plan
            out.append(entry)
        return out


# ---------- clarify request (same pattern, returns text) ----------


def request_clarify(
    auth_sid: str,
    loop: asyncio.AbstractEventLoop,
    question: str,
    choices: list[str] | None = None,
) -> str:
    """Block until the user answers. Returns the answer text.

    On timeout returns empty string. Empty string is the documented
    'gateway-can't-answer' signal — caller picks default and proceeds.
    """
    request_id = uuid.uuid4().hex
    ev = threading.Event()
    with _state_lock:
        _pending_clarifies[request_id] = {
            "event": ev,
            "answer": None,
            "question": question,
            "choices": list(choices or []),
            "auth_sid": auth_sid,
            "ts": time.time(),
        }
    _broadcast_from_thread(loop, auth_sid, {
        "type": "clarify_pending",
        "request_id": request_id,
        "question": question,
        "choices": list(choices or []),
    })
    timed_out = not ev.wait(timeout=_clarify_timeout())
    with _state_lock:
        entry = _pending_clarifies.pop(request_id, None)
    if entry is None or timed_out or entry["answer"] is None:
        answer = ""
    else:
        answer = str(entry["answer"])
    _broadcast_from_thread(loop, auth_sid, {
        "type": "clarify_resolved",
        "request_id": request_id,
        "answer": answer,
        "timed_out": timed_out,
    })
    return answer


def resolve_clarify(request_id: str, answer: str) -> bool:
    with _state_lock:
        entry = _pending_clarifies.get(request_id)
        if entry is None:
            return False
        entry["answer"] = str(answer)
        entry["event"].set()
        return True


def list_pending_clarifies(auth_sid: str = "") -> list[dict]:
    with _state_lock:
        return [
            {
                "request_id": rid,
                "question": e["question"],
                "choices": list(e["choices"]),
                "ts": e["ts"],
            }
            for rid, e in _pending_clarifies.items()
            if not auth_sid or e["auth_sid"] == auth_sid
        ]


# ---------- test helpers ----------


def _reset_for_tests() -> None:
    """Pytest helper — clear all bridge state."""
    with _state_lock:
        for entry in _pending_approvals.values():
            entry["event"].set()
        for entry in _pending_clarifies.values():
            entry["event"].set()
        _pending_approvals.clear()
        _pending_clarifies.clear()
        _event_subscribers.clear()
