"""Tests for v1.40.2 — a2a_call client tool (Phase 10.4.2)."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from janus.tools import a2a_call as ac, default_registry


# ---------- registry ----------


def test_a2a_call_in_default_registry():
    assert "a2a_call" in default_registry().names()


def test_a2a_call_dangerous_exec():
    t = ac.A2ACall()
    assert t.dangerous is True
    assert t.risk == "exec"


def test_a2a_call_schema():
    s = ac.A2ACall().schema()["function"]
    assert s["name"] == "a2a_call"
    assert s["parameters"]["required"] == ["agent_url", "prompt"]
    for k in ("agent_url", "prompt", "bearer_token", "timeout"):
        assert k in s["parameters"]["properties"]


# ---------- early validation ----------


def test_empty_agent_url_rejected():
    out = ac.A2ACall().run({"agent_url": "", "prompt": "x"}, lambda *a, **kw: True)
    assert "agent_url" in out.lower()


def test_empty_prompt_rejected():
    out = ac.A2ACall().run(
        {"agent_url": "https://x", "prompt": ""}, lambda *a, **kw: True,
    )
    assert "empty prompt" in out.lower()


def test_non_http_url_rejected():
    out = ac.A2ACall().run(
        {"agent_url": "ftp://nope", "prompt": "x"}, lambda *a, **kw: True,
    )
    assert "http(s)" in out.lower()


def test_normalize_url():
    assert ac._normalize_url("https://x.com/") == "https://x.com"
    assert ac._normalize_url("https://x.com////") == "https://x.com"
    assert ac._normalize_url("https://x.com") == "https://x.com"


# ---------- approver ----------


def test_approver_refusal_short_circuits(monkeypatch):
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: False,
    )
    assert "refused" in out.lower()


def test_approver_capability_token():
    seen = {}

    def app(action, details, **kw):
        seen["cap"] = kw.get("capability")
        return False

    ac.A2ACall().run({"agent_url": "https://x.com", "prompt": "x"}, app)
    assert seen["cap"] == ("a2a", "call", "https://x.com")


# ---------- happy path ----------


def _patch_fetch(monkeypatch, responses):
    """Pin two response queue (discovery first, then tasks/send)."""
    queue = list(responses)

    def fake_fetch(url, *, headers, body=None, timeout=60):
        return queue.pop(0)

    monkeypatch.setattr(ac, "_fetch_json", fake_fetch)


def test_happy_path(monkeypatch):
    card = {"name": "Other", "authentication": {"schemes": ["bearer"]}}
    task_response = {
        "jsonrpc": "2.0",
        "result": {
            "id": "tid",
            "state": "completed",
            "artifacts": [{
                "name": "response",
                "parts": [{"type": "text", "text": "hello back"}],
            }],
        },
        "id": 1,
    }
    _patch_fetch(monkeypatch, [
        (200, card, json.dumps(card)),
        (200, task_response, json.dumps(task_response)),
    ])
    out = ac.A2ACall().run(
        {
            "agent_url": "https://other.example",
            "prompt": "ping",
            "bearer_token": "tok",
        },
        lambda *a, **kw: True,
    )
    assert out == "hello back"


def test_extract_artifact_text_helper():
    task = {
        "artifacts": [
            {"parts": [{"type": "text", "text": "first"}]},
            {"parts": [{"type": "text", "text": "second"}]},
        ],
    }
    # Most recent is last entry
    assert ac._extract_artifact_text(task) == "second"


def test_extract_artifact_text_falls_back_to_message():
    task = {
        "artifacts": [],
        "message": {
            "role": "agent",
            "parts": [{"type": "text", "text": "just-msg"}],
        },
    }
    assert ac._extract_artifact_text(task) == "just-msg"


def test_extract_artifact_text_empty():
    assert ac._extract_artifact_text({}) == ""


# ---------- failure paths ----------


def test_discovery_network_error(monkeypatch):
    _patch_fetch(monkeypatch, [(0, None, "network error: timed out")])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "discovery failed" in out.lower()


def test_discovery_404(monkeypatch):
    _patch_fetch(monkeypatch, [(404, None, "not found")])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "404" in out


def test_discovery_returns_non_dict(monkeypatch):
    _patch_fetch(monkeypatch, [(200, None, "<html>not json</html>")])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "not a json object" in out.lower()


def test_tasks_send_401_with_helpful_message(monkeypatch):
    card = {"name": "Other", "authentication": {"schemes": ["bearer"]}}
    _patch_fetch(monkeypatch, [
        (200, card, json.dumps(card)),
        (401, {"error": "no auth"}, "no auth"),
    ])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "401" in out
    assert "bearer" in out.lower()


def test_tasks_send_jsonrpc_error(monkeypatch):
    card = {"name": "Other"}
    err_resp = {
        "jsonrpc": "2.0",
        "error": {"code": -32601, "message": "Method not found"},
        "id": 1,
    }
    _patch_fetch(monkeypatch, [
        (200, card, json.dumps(card)),
        (200, err_resp, json.dumps(err_resp)),
    ])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "JSON-RPC error" in out
    assert "Method not found" in out


def test_tasks_send_failed_state(monkeypatch):
    card = {"name": "Other"}
    failed_resp = {
        "jsonrpc": "2.0",
        "result": {
            "id": "x",
            "state": "failed",
            "artifacts": [],
            "message": {
                "role": "agent",
                "parts": [{"type": "text", "text": "broke"}],
            },
        },
        "id": 1,
    }
    _patch_fetch(monkeypatch, [
        (200, card, json.dumps(card)),
        (200, failed_resp, json.dumps(failed_resp)),
    ])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "failed" in out.lower()
    assert "broke" in out


def test_tasks_send_non_terminal_state(monkeypatch):
    card = {"name": "Other"}
    pending_resp = {
        "jsonrpc": "2.0",
        "result": {"id": "tid-pending", "state": "working"},
        "id": 1,
    }
    _patch_fetch(monkeypatch, [
        (200, card, json.dumps(card)),
        (200, pending_resp, json.dumps(pending_resp)),
    ])
    out = ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x"},
        lambda *a, **kw: True,
    )
    assert "non-terminal" in out.lower() or "working" in out.lower()
    assert "tid-pending" in out


def test_bearer_token_added_to_headers(monkeypatch):
    """Pin: bearer_token arg becomes Authorization header on tasks/send."""
    card = {"name": "Other"}
    captured = {}

    def fake_fetch(url, *, headers, body=None, timeout=60):
        captured.setdefault("calls", []).append({
            "url": url, "headers": dict(headers),
        })
        if "/a2a" in url and body is not None:
            return (200, {
                "jsonrpc": "2.0",
                "result": {"state": "completed", "artifacts": [{
                    "parts": [{"type": "text", "text": "ok"}],
                }]},
                "id": 1,
            }, "{}")
        return (200, card, json.dumps(card))

    monkeypatch.setattr(ac, "_fetch_json", fake_fetch)
    ac.A2ACall().run(
        {
            "agent_url": "https://x.com",
            "prompt": "x",
            "bearer_token": "secret-tok",
        },
        lambda *a, **kw: True,
    )
    # Find the /a2a POST call (skip discovery)
    post_call = next(c for c in captured["calls"] if c["url"].endswith("/a2a"))
    assert post_call["headers"].get("Authorization") == "Bearer secret-tok"


# ---------- timeout clamp ----------


def test_default_timeout_is_60():
    """Pin via constants — 60s default keeps simple cases snappy."""
    assert ac.DEFAULT_TIMEOUT == 60


def test_timeout_clamp(monkeypatch):
    """Pass a huge timeout; assert it's capped before reaching urllib."""
    monkeypatch.setattr(ac, "TIMEOUT_MAX", 300)
    captured = {}

    def fake_fetch(url, *, headers, body=None, timeout=60):
        captured["timeout"] = timeout
        if body is None:
            return (200, {"name": "x"}, "{}")
        return (200, {
            "jsonrpc": "2.0",
            "result": {"state": "completed", "artifacts": [
                {"parts": [{"type": "text", "text": "ok"}]},
            ]},
            "id": 1,
        }, "{}")

    monkeypatch.setattr(ac, "_fetch_json", fake_fetch)
    ac.A2ACall().run(
        {"agent_url": "https://x.com", "prompt": "x", "timeout": 99999},
        lambda *a, **kw: True,
    )
    assert captured["timeout"] == 300


# ---------- version ----------


def test_version_bumped_to_1_40_2():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 40, 2)
