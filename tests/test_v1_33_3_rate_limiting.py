"""Tests for v1.33.3 — public endpoint rate limiting (Phase 6.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from janus import web_rate_limit as rl


# -------------------- TokenBucketLimiter --------------------


def test_consume_allowed_when_full():
    limiter = rl.TokenBucketLimiter(capacity=5, refill_per_second=0.0)
    allowed, retry = limiter.consume("ip-1", now=0.0)
    assert allowed is True
    assert retry == 0.0


def test_burst_then_blocked():
    """Capacity=5: 5 requests succeed, 6th blocked."""
    limiter = rl.TokenBucketLimiter(capacity=5, refill_per_second=0.0)
    for i in range(5):
        allowed, _ = limiter.consume("ip-1", now=0.0)
        assert allowed is True, f"request {i} unexpectedly blocked"
    allowed, retry = limiter.consume("ip-1", now=0.0)
    assert allowed is False
    assert retry > 0


def test_separate_keys_separate_buckets():
    limiter = rl.TokenBucketLimiter(capacity=2, refill_per_second=0.0)
    a1, _ = limiter.consume("a", now=0.0)
    a2, _ = limiter.consume("a", now=0.0)
    a3, _ = limiter.consume("a", now=0.0)
    b1, _ = limiter.consume("b", now=0.0)
    assert (a1, a2, a3) == (True, True, False)
    assert b1 is True  # b's bucket independent


def test_refill_over_time():
    """1 token/sec refill — after 3 seconds, 3 more requests allowed."""
    limiter = rl.TokenBucketLimiter(capacity=2, refill_per_second=1.0)
    limiter.consume("k", now=0.0)
    limiter.consume("k", now=0.0)
    blocked, _ = limiter.consume("k", now=0.0)
    assert blocked is False
    # After 3s, bucket is refilled (capped at capacity=2)
    a, _ = limiter.consume("k", now=3.0)
    b, _ = limiter.consume("k", now=3.0)
    c, _ = limiter.consume("k", now=3.0)
    assert (a, b, c) == (True, True, False)


def test_refill_capped_at_capacity():
    """Idle long enough for unlimited refill — bucket caps at capacity."""
    limiter = rl.TokenBucketLimiter(capacity=3, refill_per_second=1.0)
    limiter.consume("k", now=0.0)  # 2 tokens left
    # Wait an hour
    a, _ = limiter.consume("k", now=3600.0)
    b, _ = limiter.consume("k", now=3600.0)
    c, _ = limiter.consume("k", now=3600.0)
    d, _ = limiter.consume("k", now=3600.0)
    assert (a, b, c, d) == (True, True, True, False)


def test_retry_after_decreases_with_partial_refill():
    limiter = rl.TokenBucketLimiter(capacity=1, refill_per_second=1.0)
    limiter.consume("k", now=0.0)
    _, retry_immediate = limiter.consume("k", now=0.0)
    _, retry_later = limiter.consume("k", now=0.5)
    # Half a second later, retry_after should be smaller
    assert retry_later < retry_immediate


def test_reset_clears_bucket():
    limiter = rl.TokenBucketLimiter(capacity=2, refill_per_second=0.0)
    limiter.consume("k", now=0.0)
    limiter.consume("k", now=0.0)
    assert limiter.consume("k", now=0.0)[0] is False
    limiter.reset("k")
    assert limiter.consume("k", now=0.0)[0] is True


def test_reset_all_clears_all_buckets():
    limiter = rl.TokenBucketLimiter(capacity=1, refill_per_second=0.0)
    limiter.consume("a", now=0.0)
    limiter.consume("b", now=0.0)
    assert limiter.consume("a", now=0.0)[0] is False
    assert limiter.consume("b", now=0.0)[0] is False
    limiter.reset(None)
    assert limiter.consume("a", now=0.0)[0] is True
    assert limiter.consume("b", now=0.0)[0] is True


# -------------------- client_ip_from_headers --------------------


def test_client_ip_prefers_x_real_ip():
    headers = {"X-Real-IP": "1.2.3.4", "X-Forwarded-For": "5.6.7.8"}
    assert rl.client_ip_from_headers(headers, fallback="9.9.9.9") == "1.2.3.4"


def test_client_ip_falls_back_to_x_forwarded_for():
    headers = {"X-Forwarded-For": "1.2.3.4, 5.6.7.8"}
    assert rl.client_ip_from_headers(headers, fallback="9.9.9.9") == "1.2.3.4"


def test_client_ip_uses_fallback_when_no_headers():
    assert rl.client_ip_from_headers({}, fallback="9.9.9.9") == "9.9.9.9"
    assert rl.client_ip_from_headers(None, fallback="9.9.9.9") == "9.9.9.9"


def test_client_ip_case_insensitive_headers():
    headers = {"x-real-ip": "lower.case.ip"}
    assert rl.client_ip_from_headers(headers, fallback="9.9.9.9") == "lower.case.ip"


def test_client_ip_empty_string_when_nothing():
    assert rl.client_ip_from_headers({}, fallback=None) == ""


# -------------------- is_rate_limited_path --------------------


def test_login_is_rate_limited():
    assert rl.is_rate_limited_path("/login") is True


def test_api_paths_are_rate_limited():
    assert rl.is_rate_limited_path("/api/chat") is True
    assert rl.is_rate_limited_path("/api/mcp/catalog") is True


def test_api_health_is_exempt():
    """Monitoring tools probe /api/health constantly — must NEVER
    rate-limit."""
    assert rl.is_rate_limited_path("/api/health") is False


def test_static_assets_not_limited():
    assert rl.is_rate_limited_path("/") is False
    assert rl.is_rate_limited_path("/static/app.js") is False
    assert rl.is_rate_limited_path("/healthz") is False


# -------------------- Module singleton --------------------


def test_get_default_limiter_returns_singleton():
    rl.reset_default_limiter()
    a = rl.get_default_limiter()
    b = rl.get_default_limiter()
    assert a is b


def test_default_limiter_uses_env_overrides(monkeypatch):
    rl.reset_default_limiter()
    monkeypatch.setenv("JANUS_RATE_LIMIT_BURST", "10")
    monkeypatch.setenv("JANUS_RATE_LIMIT_REFILL_PER_SECOND", "0.5")
    limiter = rl.get_default_limiter()
    assert limiter.capacity == 10
    assert limiter.refill_per_second == 0.5
    rl.reset_default_limiter()


# -------------------- Web middleware behavioral test --------------------


def test_login_returns_429_after_burst():
    """End-to-end: hammer /login through TestClient, see 429 + Retry-After."""
    pytest.importorskip("fastapi")
    import os
    os.environ["JANUS_WEB_LOCALHOST_NO_AUTH"] = "1"
    os.environ["JANUS_RATE_LIMIT_BURST"] = "3"
    os.environ["JANUS_RATE_LIMIT_REFILL_PER_SECOND"] = "0.0"
    rl.reset_default_limiter()
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    # First 3 hits: any status (token / cookie state varies).
    for _ in range(3):
        client.post("/login", data={"token": "wrong"})
    # 4th hit: 429.
    resp = client.post("/login", data={"token": "wrong"})
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body.get("error") == "rate limit exceeded"
    # cleanup
    rl.reset_default_limiter()
    os.environ.pop("JANUS_RATE_LIMIT_BURST", None)
    os.environ.pop("JANUS_RATE_LIMIT_REFILL_PER_SECOND", None)


def test_health_endpoint_not_rate_limited():
    """Monitoring tools must NOT get 429s on /api/health, even
    after exceeding the burst limit elsewhere."""
    pytest.importorskip("fastapi")
    import os
    os.environ["JANUS_WEB_LOCALHOST_NO_AUTH"] = "1"
    os.environ["JANUS_RATE_LIMIT_BURST"] = "1"
    os.environ["JANUS_RATE_LIMIT_REFILL_PER_SECOND"] = "0.0"
    rl.reset_default_limiter()
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.gateways import web as web_module
    importlib.reload(web_module)
    app = web_module._build_app()
    from fastapi.testclient import TestClient
    client = TestClient(app)
    for _ in range(5):
        resp = client.get("/api/health")
        assert resp.status_code == 200, (
            f"/api/health rate-limited at iteration; status {resp.status_code}"
        )
    rl.reset_default_limiter()
    os.environ.pop("JANUS_RATE_LIMIT_BURST", None)
    os.environ.pop("JANUS_RATE_LIMIT_REFILL_PER_SECOND", None)


# -------------------- Source pin: middleware wired --------------------


def test_web_middleware_present():
    web_path = (
        Path(__file__).parent.parent / "janus" / "gateways" / "web.py"
    )
    src = web_path.read_text(encoding="utf-8")
    assert "_rate_limit_mw" in src
    assert "from .. import web_rate_limit" in src
    assert 'app.middleware("http")' in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_33_3_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 33, 3)
