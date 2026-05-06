"""Tests for v1.22.0a async approval/clarify bridge.

The bridge lets the synchronous executor.chat approver block on a
threading.Event while a FastAPI handler delivers the request to the
browser via SSE and a follow-up POST to /api/approve resolves the
event. These tests cover the bridge primitives + end-to-end flow.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest


try:
    from janus.gateways import web_bridge
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    from janus.gateways import web_auth
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


pytestmark = pytest.mark.skipif(
    not _HAS_FASTAPI, reason="fastapi not installed",
)


@pytest.fixture(autouse=True)
def _reset_bridge():
    web_bridge._reset_for_tests()
    yield
    web_bridge._reset_for_tests()


# ---------- bridge primitives ----------


def test_approval_request_blocks_until_resolved():
    """The approver thread blocks until resolve_approval fires the event."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    decision_holder = {}

    def worker():
        decision_holder["d"] = web_bridge.request_approval(
            auth_sid="alice",
            loop=loop,
            label="rm /tmp/x",
            details="dangerous",
            risk="exec",
        )

    t = threading.Thread(target=worker)
    t.start()

    # Give the worker a moment to register the pending approval.
    time.sleep(0.05)
    pending = web_bridge.list_pending_approvals("alice")
    assert len(pending) == 1
    request_id = pending[0]["request_id"]

    # Resolve it.
    assert web_bridge.resolve_approval(request_id, True) is True
    t.join(timeout=2.0)
    assert decision_holder["d"] is True

    loop.close()


def test_approval_resolve_unknown_returns_false():
    assert web_bridge.resolve_approval("not-a-real-id", True) is False


def test_approval_timeout(monkeypatch):
    """If nobody resolves within timeout, request_approval returns False."""
    monkeypatch.setenv("JANUS_WEB_APPROVAL_TIMEOUT", "0.2")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    result = web_bridge.request_approval(
        auth_sid="alice", loop=loop,
        label="x", details="y", risk="exec",
    )
    assert result is False
    # Pending entry must be cleared after timeout.
    assert web_bridge.list_pending_approvals("alice") == []
    loop.close()


def test_clarify_round_trip():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    answer_holder = {}

    def worker():
        answer_holder["a"] = web_bridge.request_clarify(
            auth_sid="bob", loop=loop,
            question="favorite color?",
            choices=["red", "blue"],
        )

    t = threading.Thread(target=worker)
    t.start()
    time.sleep(0.05)
    pending = web_bridge.list_pending_clarifies("bob")
    assert len(pending) == 1
    rid = pending[0]["request_id"]
    web_bridge.resolve_clarify(rid, "blue")
    t.join(timeout=2.0)
    assert answer_holder["a"] == "blue"
    loop.close()


def test_clarify_timeout_returns_empty(monkeypatch):
    monkeypatch.setenv("JANUS_WEB_CLARIFY_TIMEOUT", "0.2")
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    result = web_bridge.request_clarify(
        auth_sid="bob", loop=loop, question="?", choices=[],
    )
    assert result == ""
    loop.close()


# ---------- subscriber lifecycle ----------


def test_subscriber_add_remove():
    assert web_bridge.subscriber_count() == 0
    q = web_bridge.add_subscriber("alice")
    assert web_bridge.subscriber_count("alice") == 1
    web_bridge.remove_subscriber("alice", q)
    assert web_bridge.subscriber_count("alice") == 0


def test_subscriber_isolation_per_sid():
    q1 = web_bridge.add_subscriber("alice")
    q2 = web_bridge.add_subscriber("bob")
    assert web_bridge.subscriber_count("alice") == 1
    assert web_bridge.subscriber_count("bob") == 1
    assert web_bridge.subscriber_count() == 2
    web_bridge.remove_subscriber("alice", q1)
    web_bridge.remove_subscriber("bob", q2)


# ---------- HTTP endpoints ----------


def _authed_client(janus_home_path=None):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200
    c.csrf_token = r.json()["csrf_token"]  # type: ignore[attr-defined]
    return c


def test_approve_requires_auth(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/api/approve/anything", json={"approve": True})
    assert r.status_code == 401


def test_approve_requires_csrf(janus_home):
    c = _authed_client(janus_home)
    # No X-CSRF-Token header.
    r = c.post("/api/approve/anything", json={"approve": True})
    assert r.status_code == 403


def test_approve_unknown_returns_404(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/approve/no-such-id",
        json={"approve": True},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 404


def test_approve_resolves_pending(janus_home):
    """End-to-end: register a pending approval, then POST /api/approve."""
    c = _authed_client(janus_home)
    # Manually inject a pending approval (simulating what a worker
    # thread would do via request_approval).
    ev = threading.Event()
    request_id = "test-req-id"
    with web_bridge._state_lock:
        web_bridge._pending_approvals[request_id] = {
            "event": ev,
            "decision": False,
            "label": "x",
            "details": "y",
            "risk": "exec",
            "auth_sid": "any",
            "ts": time.time(),
        }
    r = c.post(
        f"/api/approve/{request_id}",
        json={"approve": True},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    assert r.json()["decision"] is True
    # Event must be set so the (hypothetical) worker would unblock.
    assert ev.is_set()


def test_clarify_resolves_pending(janus_home):
    c = _authed_client(janus_home)
    ev = threading.Event()
    request_id = "test-clarify-id"
    with web_bridge._state_lock:
        web_bridge._pending_clarifies[request_id] = {
            "event": ev,
            "answer": None,
            "question": "?",
            "choices": [],
            "auth_sid": "any",
            "ts": time.time(),
        }
    r = c.post(
        f"/api/clarify/{request_id}",
        json={"answer": "yes"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    assert ev.is_set()


def test_events_endpoint_requires_auth(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/events")
    assert r.status_code == 401


# ---------- end-to-end approver wiring ----------


def test_make_web_approver_falls_back_to_deny_without_bridge(janus_home):
    """Legacy callers (no auth_sid/loop) get the v1.21 deny-on-ASK behavior."""
    approver = web_mod._make_web_approver("default")
    # mode=default + risk=exec → ASK → returns False (deny) when no bridge.
    decision = approver("write x.py", "details", risk="exec")
    assert decision is False


def test_make_web_approver_allows_when_mode_permits(janus_home):
    approver = web_mod._make_web_approver("bypassPermissions")
    # bypass mode → ALLOW for everything.
    assert approver("anything", "x", risk="exec") is True


def test_make_web_clarify_callback_handles_missing_bridge(janus_home):
    """No auth_sid/loop → returns the documented [clarify unavailable] string."""
    cb = web_mod._make_web_clarify_callback("", None)
    result = cb("question?", None)
    assert "[clarify unavailable" in result
