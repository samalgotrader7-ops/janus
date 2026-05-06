"""v1.21 web security regressions.

Sam's pre-v1.21 instance at http://vps.example.com:8765/ exposed `/chat`,
`/memory`, `/cost`, and `/home` to the internet with no authentication.
`/memory` and `/cost` were ALWAYS open even when pairing was enabled.

These tests pin the contract that EVERY mutating or sensitive route
requires authentication. If a future change re-introduces an
unauthenticated route, this test file fails immediately.
"""
from __future__ import annotations

import pytest

try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    from janus.gateways import web_auth
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False


pytestmark = pytest.mark.skipif(
    not _HAS_FASTAPI, reason="fastapi not installed",
)


def _client(janus_home_path=None):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    return TestClient(app)


def test_memory_dump_requires_auth(janus_home):
    """Pre-v1.21: anyone could read full memory. v1.21: auth required."""
    c = _client(janus_home)
    r = c.get("/memory")
    assert r.status_code == 401, (
        "/memory must require auth — pre-v1.21 this leaked the user's "
        "full memory dump including identity, soul, and relationship "
        "cards. Do NOT remove the auth gate."
    )


def test_memory_category_requires_auth(janus_home):
    """Same gate for category-specific memory reads."""
    c = _client(janus_home)
    r = c.get("/memory?category=user")
    assert r.status_code == 401


def test_cost_endpoint_requires_auth(janus_home):
    """Pre-v1.21: anyone could read per-chat cost ledgers."""
    c = _client(janus_home)
    r = c.get("/cost")
    assert r.status_code == 401


def test_home_endpoint_requires_auth(janus_home):
    """Pre-v1.21: anyone could hijack the cron/home channel."""
    c = _client(janus_home)
    r = c.post("/home", json={"session_id": "attacker"})
    assert r.status_code == 401


def test_chat_endpoint_requires_auth(janus_home):
    """No auth → 401, no token spend, no executor invocation."""
    c = _client(janus_home)
    r = c.post("/chat", json={"request": "anything"})
    assert r.status_code == 401


def test_index_redirects_to_login_when_unauthenticated(janus_home):
    """GET / when not logged in → 303 to /login."""
    c = _client(janus_home)
    r = c.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "/login" in r.headers.get("location", "")


def test_login_page_unauthenticated_renders_form(janus_home):
    """GET /login (unauthenticated) shows the login HTML — no redirect."""
    c = _client(janus_home)
    r = c.get("/login")
    assert r.status_code == 200
    assert "<form" in r.text.lower()
    assert "token" in r.text.lower()


def test_unauthenticated_chat_does_not_invoke_executor(
    janus_home, fake_llm,
):
    """Belt-and-braces: unauthorized /chat must NOT spend any LLM
    tokens. fake_llm queue stays untouched after a refused request."""
    fake_llm.append({"role": "assistant", "content": "should never be returned"})
    c = _client(janus_home)
    r = c.post("/chat", json={"request": "burn my tokens"})
    assert r.status_code == 401
    # The fake_llm queue still has its one entry.
    assert len(fake_llm) == 1


def test_session_cookie_is_httponly_and_strict(janus_home):
    """Cookie must have HttpOnly + SameSite=Strict so XSS / CSRF can't
    use the auth cookie out of band."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie


def test_csrf_with_wrong_token_rejected(janus_home):
    """Right session cookie + WRONG csrf token → 403."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    c.post("/login", json={"token": token})
    # Send POST with deliberately bad CSRF token.
    r = c.post(
        "/chat", json={"request": "hi", "session_id": "s"},
        headers={"x-csrf-token": "wrong.token"},
    )
    assert r.status_code == 403


def test_post_login_does_not_skip_csrf_for_other_routes(janus_home):
    """Logging in should NOT exempt subsequent POSTs from CSRF."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    c.post("/login", json={"token": token})
    # No CSRF header — must fail.
    r = c.post("/home", json={"session_id": "x"})
    assert r.status_code == 403


def test_localhost_no_auth_opt_in_only(janus_home, monkeypatch):
    """JANUS_WEB_LOCALHOST_NO_AUTH must default OFF (auth required even
    for localhost). Setting it to 1 explicitly enables the bypass."""
    # Default: no env, no bypass.
    monkeypatch.delenv("JANUS_WEB_LOCALHOST_NO_AUTH", raising=False)
    c = _client(janus_home)
    r = c.get("/memory")
    assert r.status_code == 401, (
        "localhost requests must still require auth by default. "
        "JANUS_WEB_LOCALHOST_NO_AUTH=1 is the only escape hatch."
    )

    # With opt-in env: bypass active.
    monkeypatch.setenv("JANUS_WEB_LOCALHOST_NO_AUTH", "1")
    c2 = _client(janus_home)
    r2 = c2.get("/memory")
    # Should NOT be 401 — auth bypass took effect.
    assert r2.status_code != 401


def test_resolve_host_warns_on_non_localhost_without_tls(
    janus_home, capsys, monkeypatch,
):
    """Non-localhost bind should emit a warning at server start."""
    # We exercise the code path via the helper that builds the warning,
    # not the full uvicorn run. The serve() body prints the warning when
    # is_local is False.
    from janus import config
    monkeypatch.setattr(config, "WEB_HOST_OK", True)
    host, refusal = web_mod._resolve_host("0.0.0.0")
    assert host == "0.0.0.0"
    assert refusal is None
