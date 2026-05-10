"""Tests for v1.37.2 — /goal cross-surface parity (Phase 10.1.2).

Coverage:
  * /api/goal/status web endpoint — null / active / paused / done
  * /chat response payload — includes 'goal' field when goal is
    active, omits when no goal
  * scope keying — web reads/writes 'web:<session_id>'
"""

from __future__ import annotations

import pytest

from janus import goals


_HAS_FASTAPI = True
try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
except ImportError:
    _HAS_FASTAPI = False


def _logged_in_client(app):
    """Login, return (client, csrf_token)."""
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200, r.text
    csrf = r.json().get("csrf_token")
    return c, csrf


# ---------- /api/goal/status ----------


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_goal_status_endpoint_no_goal(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c, _csrf = _logged_in_client(app)
    r = c.get("/api/goal/status?session_id=s1")
    assert r.status_code == 200
    assert r.json() == {"goal": None}


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_goal_status_endpoint_active(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c, _csrf = _logged_in_client(app)
    goals.set_goal("web:s2", "ship the thing", turn_budget=42)
    r = c.get("/api/goal/status?session_id=s2")
    assert r.status_code == 200
    body = r.json()
    g = body["goal"]
    assert g["text"] == "ship the thing"
    assert g["status"] == "active"
    assert g["turn_budget"] == 42
    assert g["turns_used"] == 0
    assert g["remaining"] == 42


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_goal_status_endpoint_paused_includes_paused_at(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c, _csrf = _logged_in_client(app)
    goals.set_goal("web:s3", "do x")
    goals.pause("web:s3")
    r = c.get("/api/goal/status?session_id=s3")
    assert r.status_code == 200
    g = r.json()["goal"]
    assert g["status"] == "paused"
    assert g["paused_at"] is not None


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_goal_status_requires_session_id(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c, _csrf = _logged_in_client(app)
    r = c.get("/api/goal/status")
    assert r.status_code == 400


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_goal_status_unauthenticated(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/goal/status?session_id=s1")
    assert r.status_code == 401


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_goal_status_scopes_per_session(janus_home):
    """Pin: /api/goal/status reads ``web:<sid>`` so two browsers
    keep distinct goals even though they share the auth login."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c, _csrf = _logged_in_client(app)
    goals.set_goal("web:alice", "alice's goal")
    goals.set_goal("web:bob", "bob's goal")
    r1 = c.get("/api/goal/status?session_id=alice")
    r2 = c.get("/api/goal/status?session_id=bob")
    assert r1.json()["goal"]["text"] == "alice's goal"
    assert r2.json()["goal"]["text"] == "bob's goal"


# ---------- /chat response goal payload ----------


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_response_includes_goal_when_active(janus_home, monkeypatch):
    """Pin: when a goal is active, /chat returns a 'goal' field
    describing the after_turn() decision (achieved/paused/continue)."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    # Patch app.run_turn so we don't make a real LLM call.
    from janus import app as janus_app
    monkeypatch.setattr(
        janus_app, "run_turn",
        lambda **kw: ("the agent's reply", []),
    )
    # Patch goal_loop.run_judge to return a deterministic verdict.
    from janus import goal_loop
    monkeypatch.setattr(
        goal_loop, "run_judge",
        lambda goal_text, last_response: goal_loop.JudgeResult(
            achieved=False, reason="wip", next_step="do next thing",
        ),
    )

    app = web_mod._build_app()
    c, csrf = _logged_in_client(app)
    sid = "chat-sess-1"
    goals.set_goal(f"web:{sid}", "ship it")
    r = c.post(
        "/chat",
        headers={"X-CSRF-Token": csrf},
        json={"request": "hello", "session_id": sid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "goal" in body, body
    assert body["goal"]["status"] == "continue"
    assert body["goal"]["next_prompt"] == "do next thing"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_response_omits_goal_when_no_goal(janus_home, monkeypatch):
    """Pin: chat without an active goal does NOT include a 'goal'
    field — backwards-compatible response shape for clients that
    don't know about the field."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    from janus import app as janus_app
    monkeypatch.setattr(
        janus_app, "run_turn",
        lambda **kw: ("the agent's reply", []),
    )

    app = web_mod._build_app()
    c, csrf = _logged_in_client(app)
    sid = "chat-sess-2"
    r = c.post(
        "/chat",
        headers={"X-CSRF-Token": csrf},
        json={"request": "hello", "session_id": sid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "goal" not in body


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_response_goal_achieved(janus_home, monkeypatch):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    from janus import app as janus_app, goal_loop
    monkeypatch.setattr(
        janus_app, "run_turn",
        lambda **kw: ("done", []),
    )
    monkeypatch.setattr(
        goal_loop, "run_judge",
        lambda goal_text, last_response: goal_loop.JudgeResult(
            achieved=True, reason="committed and pushed", next_step="",
        ),
    )

    app = web_mod._build_app()
    c, csrf = _logged_in_client(app)
    sid = "chat-sess-3"
    goals.set_goal(f"web:{sid}", "ship it")
    r = c.post(
        "/chat",
        headers={"X-CSRF-Token": csrf},
        json={"request": "ok", "session_id": sid},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["goal"]["status"] == "achieved"
    # Goal state on disk is now done
    g = goals.load(f"web:{sid}")
    assert g.status == "done"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_response_goal_budget_exhausted(janus_home, monkeypatch):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    from janus import app as janus_app, goal_loop
    monkeypatch.setattr(
        janus_app, "run_turn",
        lambda **kw: ("again", []),
    )
    monkeypatch.setattr(
        goal_loop, "run_judge",
        lambda goal_text, last_response: goal_loop.JudgeResult(
            achieved=False, reason="not yet", next_step="more work",
        ),
    )

    app = web_mod._build_app()
    c, csrf = _logged_in_client(app)
    sid = "chat-sess-4"
    g = goals.set_goal(f"web:{sid}", "x", turn_budget=1)
    # Already at budget — single turn pushes over
    r = c.post(
        "/chat",
        headers={"X-CSRF-Token": csrf},
        json={"request": "ok", "session_id": sid},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["goal"]["status"] == "paused"
    assert body["goal"]["marker"] == "budget"


# ---------- version ----------


def test_version_bumped_to_1_37_2():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 37, 2)
