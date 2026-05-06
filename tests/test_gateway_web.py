"""Tests for the web gateway (v1.0 chat-shaped).

The web module lazy-imports FastAPI; tests skip cleanly when it isn't
installed. We verify:
  - host/port resolution and the non-localhost refusal rule
  - that serve() exits cleanly when FastAPI is missing
  - (when FastAPI is present) GET /, GET /favicon.svg, POST /chat
"""
from __future__ import annotations
import pytest

from janus import config
from janus.gateways import web as web_mod


def test_resolve_host_localhost_default(janus_home):
    host, refusal = web_mod._resolve_host(None)
    assert host in ("127.0.0.1", "localhost", "::1")
    assert refusal is None


def test_resolve_host_explicit_localhost_ok(janus_home):
    host, refusal = web_mod._resolve_host("127.0.0.1")
    assert refusal is None


def test_resolve_host_refuses_non_localhost_without_env(janus_home, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST_OK", False)
    host, refusal = web_mod._resolve_host("0.0.0.0")
    assert host == "0.0.0.0"
    assert refusal is not None
    assert "JANUS_WEB_HOST_OK" in refusal


def test_resolve_host_allows_non_localhost_with_env(janus_home, monkeypatch):
    monkeypatch.setattr(config, "WEB_HOST_OK", True)
    host, refusal = web_mod._resolve_host("0.0.0.0")
    assert host == "0.0.0.0"
    assert refusal is None


def test_serve_without_fastapi_returns_clear_error(janus_home, monkeypatch, capsys):
    monkeypatch.setattr(web_mod, "_try_import_fastapi", lambda: None)
    rc = web_mod.serve()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FastAPI not installed" in out


def test_serve_refuses_non_localhost_without_env(janus_home, monkeypatch, capsys):
    # Stub fastapi import so we get past the dep check. v1.0 _try_import_fastapi
    # returns a 5-tuple (FastAPI, Body, HTMLResponse, JSONResponse, uvicorn).
    monkeypatch.setattr(
        web_mod, "_try_import_fastapi",
        lambda: ("a", "b", "c", "d", _FakeUvicorn()),
    )
    monkeypatch.setattr(config, "WEB_HOST_OK", False)
    monkeypatch.setattr(config, "API_KEY", "test")  # assert_configured() pass
    rc = web_mod.serve(host="0.0.0.0")
    out = capsys.readouterr().out
    assert rc == 2
    assert "JANUS_WEB_HOST_OK" in out


# Helper: pretend uvicorn so serve() doesn't actually start.
class _FakeUvicorn:
    def run(self, app, host=None, port=None, log_level=None):
        pass


# ---------- Live FastAPI tests (only if FastAPI installed) ----------


_HAS_FASTAPI = web_mod._try_import_fastapi() is not None


def _authed_client(janus_home_path=None):
    """v1.21: TestClient with a logged-in session cookie + CSRF token.

    Each test that hits authenticated routes uses this helper. The
    `csrf_token` attribute on the returned client is what callers must
    send as `X-CSRF-Token` on POSTs.
    """
    from fastapi.testclient import TestClient
    from janus.gateways import web_auth, web_audit
    # Reset rate limiter + login throttle state so tests don't poison
    # each other.
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    c.csrf_token = r.json()["csrf_token"]  # type: ignore[attr-defined]
    return c


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_index_page_renders(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/")
    assert r.status_code == 200
    body = r.text
    assert "janus" in body.lower()
    # Concept-B SVG logo is inline in the header.
    assert "<svg" in body
    assert 'href="/favicon.svg"' in body
    # Tagline reaches the page.
    from janus import branding
    assert branding.TAGLINE in body
    # v1.21: CSRF token embedded as <meta>.
    assert 'name="csrf-token"' in body


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_favicon_route_serves_svg(janus_home):
    # Favicon doesn't require auth (assets are public).
    from fastapi.testclient import TestClient
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/favicon.svg")
    assert r.status_code == 200
    assert "image/svg+xml" in r.headers.get("content-type", "")
    body = r.text
    assert body.startswith("<svg")
    assert body.rstrip().endswith("</svg>")
    # Favicon uses literal brand color (browsers ignore page CSS for favicons).
    from janus import branding
    assert branding.BRAND_COLOR in body


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_endpoint_returns_output(janus_home, fake_llm):
    """v1.3: POST /chat drives executor.chat() against a session-scoped
    messages list. First turn prepends a self-intro from soul.md + user.md;
    the assistant's text follows after a blank line."""
    fake_llm.append({"role": "assistant", "content": "hi back"})
    web_mod._SESSIONS.clear()
    c = _authed_client(janus_home)
    r = c.post(
        "/chat", json={"request": "hi", "session_id": "s1"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    data = r.json()
    # v1.3 self-intro is prepended on the first turn — "hi back" still in there.
    assert "hi back" in data["output"]
    assert data["session_id"] == "s1"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_endpoint_rejects_empty_request(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/chat", json={"request": ""},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    assert "error" in r.json()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_endpoint_keeps_session_history(janus_home, fake_llm):
    """Two requests with the same session_id should accumulate messages
    in the per-session list — second turn's context includes the first."""
    fake_llm.append({"role": "assistant", "content": "first"})
    fake_llm.append({"role": "assistant", "content": "second"})
    # Reset session store so prior tests don't pollute.
    web_mod._SESSIONS.clear()

    c = _authed_client(janus_home)
    c.post(
        "/chat", json={"request": "one", "session_id": "abc"},
        headers={"x-csrf-token": c.csrf_token},
    )
    c.post(
        "/chat", json={"request": "two", "session_id": "abc"},
        headers={"x-csrf-token": c.csrf_token},
    )

    msgs = web_mod._SESSIONS["abc"]
    # system + user1 + assistant1 + user2 + assistant2
    assert len(msgs) == 5
    assert msgs[1]["content"] == "one"
    assert msgs[3]["content"] == "two"
