"""Tests for v1.21 web UI authentication primitives.

Covers the web_auth module directly (no FastAPI) plus a few end-to-end
smoke tests via TestClient. Security regressions live in
test_web_security_regress.py.
"""
from __future__ import annotations

import os
import time

import pytest

from janus.gateways import web_auth


# ---------- bootstrap token ----------


def test_bootstrap_token_generated_on_first_call(janus_home):
    p = web_auth._token_path()
    if p.exists():
        p.unlink()
    tok = web_auth.get_or_create_bootstrap_token()
    assert tok
    assert len(tok) >= 40, "token should be at least 40 chars (URL-safe b64 of 32 bytes)"
    # Persisted to disk.
    assert p.exists()
    assert p.read_text(encoding="utf-8").strip() == tok


def test_bootstrap_token_idempotent(janus_home):
    """Calling get_or_create_bootstrap_token twice returns the same token."""
    p = web_auth._token_path()
    if p.exists():
        p.unlink()
    a = web_auth.get_or_create_bootstrap_token()
    b = web_auth.get_or_create_bootstrap_token()
    assert a == b


def test_bootstrap_token_mode_0600_on_posix(janus_home):
    """Token file should be readable only by owner."""
    if os.name != "posix":
        pytest.skip("POSIX-only file modes")
    p = web_auth._token_path()
    if p.exists():
        p.unlink()
    web_auth.get_or_create_bootstrap_token()
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


def test_rotate_bootstrap_token_replaces(janus_home):
    a = web_auth.get_or_create_bootstrap_token()
    b = web_auth.rotate_bootstrap_token()
    assert a != b
    # Subsequent reads return the new one.
    assert web_auth.get_or_create_bootstrap_token() == b


def test_verify_bootstrap_token_constant_time(janus_home):
    tok = web_auth.get_or_create_bootstrap_token()
    assert web_auth.verify_bootstrap_token(tok) is True
    assert web_auth.verify_bootstrap_token("wrong") is False
    assert web_auth.verify_bootstrap_token("") is False
    assert web_auth.verify_bootstrap_token(tok[:-1] + "x") is False


# ---------- HMAC secret ----------


def test_secret_generated_on_first_call(janus_home):
    p = web_auth._secret_path()
    if p.exists():
        p.unlink()
    key = web_auth.get_or_create_secret()
    assert isinstance(key, bytes)
    assert len(key) == 32
    assert p.exists()


def test_secret_idempotent(janus_home):
    p = web_auth._secret_path()
    if p.exists():
        p.unlink()
    a = web_auth.get_or_create_secret()
    b = web_auth.get_or_create_secret()
    assert a == b


# ---------- session signing ----------


def test_sign_and_verify_round_trip(janus_home):
    cookie = web_auth.sign_session("test-sid")
    sid = web_auth.verify_session(cookie)
    assert sid == "test-sid"


def test_verify_session_rejects_tampering(janus_home):
    cookie = web_auth.sign_session("test-sid")
    # Flip one byte in the signature half — verification must fail.
    parts = cookie.split("|")
    parts[2] = ("a" if parts[2][0] != "a" else "b") + parts[2][1:]
    tampered = "|".join(parts)
    assert web_auth.verify_session(tampered) is None


def test_verify_session_rejects_expired(janus_home):
    cookie = web_auth.sign_session("test-sid", expires_unix=int(time.time()) - 10)
    assert web_auth.verify_session(cookie) is None


def test_verify_session_rejects_malformed(janus_home):
    assert web_auth.verify_session("") is None
    assert web_auth.verify_session("not-a-cookie") is None
    assert web_auth.verify_session("a|b") is None  # too few parts
    assert web_auth.verify_session("a|notnum|sig") is None  # bad expires


def test_verify_session_rejects_swapped_sid(janus_home):
    """Substituting a different sid into a valid cookie's payload must fail."""
    cookie = web_auth.sign_session("alice")
    parts = cookie.split("|")
    swapped = "|".join(["bob", parts[1], parts[2]])
    assert web_auth.verify_session(swapped) is None


# ---------- CSRF tokens ----------


def test_csrf_round_trip(janus_home):
    sid = "abc123"
    token = web_auth.make_csrf_token(sid)
    assert web_auth.verify_csrf(sid, token) is True


def test_csrf_rejects_wrong_sid(janus_home):
    token = web_auth.make_csrf_token("alice")
    assert web_auth.verify_csrf("bob", token) is False


def test_csrf_rejects_tampered(janus_home):
    sid = "abc"
    token = web_auth.make_csrf_token(sid)
    nonce, _, sig = token.partition(".")
    tampered_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
    assert web_auth.verify_csrf(sid, f"{nonce}.{tampered_sig}") is False


def test_csrf_rejects_empty(janus_home):
    assert web_auth.verify_csrf("sid", "") is False
    assert web_auth.verify_csrf("sid", "no-dot") is False


# ---------- rate limiting ----------


def test_rate_limit_allows_under_capacity(janus_home):
    web_auth.rate_limit_reset()
    for _ in range(5):
        ok, _ = web_auth.rate_limit_take("k1", "chat")
        assert ok is True


def test_rate_limit_blocks_over_capacity(janus_home):
    web_auth.rate_limit_reset()
    # chat capacity is 30. Burst 30, then the 31st must fail.
    for i in range(30):
        ok, _ = web_auth.rate_limit_take("k2", "chat")
        assert ok is True, f"failed at {i}"
    ok, retry = web_auth.rate_limit_take("k2", "chat")
    assert ok is False
    assert retry > 0


def test_rate_limit_per_key(janus_home):
    """Two different keys have independent buckets."""
    web_auth.rate_limit_reset()
    for _ in range(30):
        web_auth.rate_limit_take("ka", "chat")
    # Bucket ka exhausted; kb still has full capacity.
    ok, _ = web_auth.rate_limit_take("kb", "chat")
    assert ok is True


# ---------- login throttle ----------


def test_login_throttle_blocks_after_threshold(janus_home, monkeypatch):
    """5 failures from one IP triggers a 15-minute block."""
    web_auth.reset_login_throttle()
    ip = "1.2.3.4"

    blocked, _ = web_auth.is_ip_blocked(ip)
    assert blocked is False

    for _ in range(5):
        web_auth.record_login_attempt(ip, success=False)

    blocked, remaining = web_auth.is_ip_blocked(ip)
    assert blocked is True
    assert remaining > 0


def test_login_throttle_resets_on_success(janus_home):
    """Success clears the failure counter."""
    web_auth.reset_login_throttle()
    ip = "5.6.7.8"
    for _ in range(4):
        web_auth.record_login_attempt(ip, success=False)
    web_auth.record_login_attempt(ip, success=True)
    # Next failure should NOT block (counter was reset).
    web_auth.record_login_attempt(ip, success=False)
    blocked, _ = web_auth.is_ip_blocked(ip)
    assert blocked is False


def test_login_throttle_per_ip(janus_home):
    """Block is per-IP — one bad actor doesn't lock out others."""
    web_auth.reset_login_throttle()
    bad_ip = "9.9.9.9"
    good_ip = "10.10.10.10"
    for _ in range(5):
        web_auth.record_login_attempt(bad_ip, success=False)

    bad_blocked, _ = web_auth.is_ip_blocked(bad_ip)
    good_blocked, _ = web_auth.is_ip_blocked(good_ip)
    assert bad_blocked is True
    assert good_blocked is False


# ---------- end-to-end via TestClient ----------


_HAS_FASTAPI = True
try:
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
except ImportError:
    _HAS_FASTAPI = False


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_login_with_correct_token_returns_csrf(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200
    data = r.json()
    assert data.get("ok") is True
    assert data.get("csrf_token")
    # Cookie should be set.
    assert web_auth.cookie_name() in r.cookies


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_login_with_wrong_token_401(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/login", json={"token": "definitely-wrong"})
    assert r.status_code == 401


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_form_login_redirects_to_index_on_success(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app, follow_redirects=False)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", data={"token": token})
    assert r.status_code == 303
    assert r.headers["location"] == "/"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_form_login_renders_error_on_failure(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/login", data={"token": "wrong"})
    assert r.status_code == 401
    assert "invalid token" in r.text.lower()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_chat_without_csrf_returns_403(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    c.post("/login", json={"token": token})
    # POST without X-CSRF-Token header — must be 403.
    r = c.post("/chat", json={"request": "hi", "session_id": "s1"})
    assert r.status_code == 403


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_logout_clears_cookie(janus_home):
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    c.post("/login", json={"token": token})
    r = c.post("/logout")
    assert r.status_code == 200
    # After logout, the cookie should be cleared (server sends Set-Cookie
    # with empty value or expires=0). The TestClient cookie jar reflects
    # the final state after the redirect/response.
    cookie_header = r.headers.get("set-cookie", "")
    assert web_auth.cookie_name() in cookie_header
    assert ("Max-Age=0" in cookie_header) or ("expires=" in cookie_header.lower())


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_login_throttle_blocks_after_failures(janus_home):
    """5 wrong tokens from same IP → 6th rejected with 429 even with right token."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    for _ in range(5):
        c.post("/login", json={"token": "wrong"})
    # 6th attempt with correct token gets blocked.
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 429
    assert "too many" in r.text.lower() or "blocked" in r.text.lower()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_healthz_unauthenticated(janus_home):
    """/healthz must respond without auth (used by load balancers)."""
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
