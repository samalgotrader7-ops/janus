"""Tests for v1.40.1 — A2A /a2a JSON-RPC tasks endpoint (Phase 10.4.1)."""

from __future__ import annotations

import pytest

from janus import a2a


@pytest.fixture(autouse=True)
def isolated_home(monkeypatch, tmp_path):
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    yield


# ---------- task storage ----------


def test_save_and_load_task():
    t = a2a.Task(id="t1", sessionId="s1")
    a2a._save_task(t)
    loaded = a2a.load_task("t1")
    assert loaded is not None
    assert loaded.id == "t1"
    assert loaded.sessionId == "s1"


def test_load_missing_task_returns_none():
    assert a2a.load_task("never-saved") is None


def test_extract_user_text_from_message():
    msg = {
        "role": "user",
        "parts": [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ],
    }
    assert a2a._extract_user_text(msg) == "hello\nworld"


def test_extract_user_text_empty():
    assert a2a._extract_user_text(None) == ""
    assert a2a._extract_user_text({}) == ""
    assert a2a._extract_user_text({"parts": []}) == ""


# ---------- JSON-RPC dispatch ----------


def _patched_run_turn(monkeypatch, output="agent reply"):
    from janus import app as janus_app
    monkeypatch.setattr(
        janus_app, "run_turn",
        lambda **kw: (output, []),
    )


def test_dispatch_tasks_send_happy_path(monkeypatch):
    _patched_run_turn(monkeypatch, output="here is the result")
    envelope = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": "task-001",
            "sessionId": "sess-001",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "do a thing"}],
            },
        },
        "id": 1,
    }
    resp = a2a.dispatch(envelope)
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    assert "result" in resp
    task = resp["result"]
    assert task["id"] == "task-001"
    assert task["state"] == a2a.STATE_COMPLETED
    assert task["artifacts"][0]["parts"][0]["text"] == "here is the result"


def test_dispatch_tasks_send_generates_id_if_missing(monkeypatch):
    _patched_run_turn(monkeypatch)
    envelope = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "x"}],
            },
        },
        "id": 2,
    }
    resp = a2a.dispatch(envelope)
    assert "result" in resp
    assert resp["result"]["id"]  # non-empty id was generated


def test_dispatch_tasks_send_missing_text_rejected(monkeypatch):
    _patched_run_turn(monkeypatch)
    envelope = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {"message": {"role": "user", "parts": []}},
        "id": 3,
    }
    resp = a2a.dispatch(envelope)
    assert "error" in resp
    assert resp["error"]["code"] == -32602  # invalid params


def test_dispatch_tasks_send_runtime_error_returns_failed(monkeypatch):
    """Pin: an exception during run_turn marks task FAILED and
    returns the task — does NOT crash with a -32603."""
    from janus import app as janus_app
    monkeypatch.setattr(
        janus_app, "run_turn",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("provider down")),
    )
    envelope = {
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": "task-fail",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "x"}],
            },
        },
        "id": 4,
    }
    resp = a2a.dispatch(envelope)
    assert "result" in resp
    assert resp["result"]["state"] == a2a.STATE_FAILED


def test_dispatch_tasks_get_after_send(monkeypatch):
    _patched_run_turn(monkeypatch, output="done")
    a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": "task-get-1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "x"}],
            },
        },
        "id": 5,
    })
    resp = a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/get",
        "params": {"id": "task-get-1"},
        "id": 6,
    })
    assert resp["result"]["state"] == a2a.STATE_COMPLETED


def test_dispatch_tasks_get_unknown_returns_error():
    resp = a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/get",
        "params": {"id": "never-existed"},
        "id": 7,
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32001


def test_dispatch_tasks_cancel_terminal_state_no_op(monkeypatch):
    _patched_run_turn(monkeypatch)
    a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/send",
        "params": {
            "id": "task-cancel-1",
            "message": {
                "role": "user",
                "parts": [{"type": "text", "text": "x"}],
            },
        },
        "id": 8,
    })
    # Task is COMPLETED — cancel returns the task unchanged
    resp = a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/cancel",
        "params": {"id": "task-cancel-1"},
        "id": 9,
    })
    assert resp["result"]["state"] == a2a.STATE_COMPLETED


def test_dispatch_tasks_cancel_unknown_returns_error():
    resp = a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/cancel",
        "params": {"id": "never"},
        "id": 10,
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32001


def test_dispatch_unknown_method():
    resp = a2a.dispatch({
        "jsonrpc": "2.0",
        "method": "tasks/dance",
        "params": {},
        "id": 11,
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_dispatch_invalid_envelope():
    assert "error" in a2a.dispatch("not a dict")
    assert a2a.dispatch({"method": "tasks/get"})["error"]["code"] == -32600  # missing jsonrpc


# ---------- web endpoint with auth ----------


_HAS_FASTAPI = True
try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
except ImportError:
    _HAS_FASTAPI = False


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_a2a_endpoint_requires_bearer(janus_home, monkeypatch):
    """Pin: with bearer auth (default), missing Authorization → 401."""
    monkeypatch.setenv("JANUS_A2A_AUTH", "bearer")
    monkeypatch.setenv("JANUS_A2A_TOKEN", "secret123")
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post(
        "/a2a",
        json={"jsonrpc": "2.0", "method": "tasks/get", "params": {}, "id": 1},
    )
    assert r.status_code == 401


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_a2a_endpoint_wrong_token_rejected(janus_home, monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "bearer")
    monkeypatch.setenv("JANUS_A2A_TOKEN", "secret123")
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post(
        "/a2a",
        headers={"Authorization": "Bearer wrong-token"},
        json={"jsonrpc": "2.0", "method": "tasks/get", "params": {}, "id": 1},
    )
    assert r.status_code == 401


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_a2a_endpoint_with_correct_token_accepts(janus_home, monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "bearer")
    monkeypatch.setenv("JANUS_A2A_TOKEN", "secret123")
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    from janus import app as janus_app
    monkeypatch.setattr(janus_app, "run_turn", lambda **kw: ("hello", []))

    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post(
        "/a2a",
        headers={"Authorization": "Bearer secret123"},
        json={
            "jsonrpc": "2.0",
            "method": "tasks/send",
            "params": {
                "message": {
                    "role": "user",
                    "parts": [{"type": "text", "text": "ping"}],
                },
            },
            "id": 1,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["state"] == a2a.STATE_COMPLETED


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_a2a_endpoint_no_auth_when_disabled(janus_home, monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "none")
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()

    from janus import app as janus_app
    monkeypatch.setattr(janus_app, "run_turn", lambda **kw: ("hi", []))

    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post(
        "/a2a",
        json={
            "jsonrpc": "2.0",
            "method": "tasks/send",
            "params": {
                "message": {"role": "user", "parts": [{"type": "text", "text": "x"}]},
            },
            "id": 1,
        },
    )
    assert r.status_code == 200


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_a2a_endpoint_misconfigured_no_token_503(janus_home, monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "bearer")
    monkeypatch.delenv("JANUS_A2A_TOKEN", raising=False)
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post(
        "/a2a",
        headers={"Authorization": "Bearer anything"},
        json={"jsonrpc": "2.0", "method": "tasks/get", "params": {}, "id": 1},
    )
    assert r.status_code == 503
    assert "misconfigured" in r.json()["error"]["message"].lower()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_a2a_endpoint_invalid_json_returns_400(janus_home, monkeypatch):
    monkeypatch.setenv("JANUS_A2A_AUTH", "none")
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/a2a", content=b"not-json")
    assert r.status_code == 400


# ---------- version ----------


def test_version_bumped_to_1_40_1():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 40, 1)
