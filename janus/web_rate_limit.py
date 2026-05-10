"""
web_rate_limit.py — token-bucket rate limiter for the web gateway
(v1.33.3, Phase 6.4).

WHY THIS EXISTS:
Phase 6 / Production hardening. Once janus-web is publicly
reachable (Phase 6.1's reverse-proxy generator made this easy),
unauthenticated endpoints like /login become attack surface for
credential stuffing / abuse. A token bucket per client IP gates
abuse without inconveniencing a real user.

BUCKET SHAPE:
  capacity = burst:    60 tokens (default)
  refill_rate:         1 token / second  (= 60/min sustained)
  Each request consumes 1 token.
  Empty bucket → 429 Too Many Requests + Retry-After header.

CLIENT IDENTITY:
  We extract the client IP from request headers in this order:
    1. X-Real-IP             (Caddy / nginx with proxy_set_header)
    2. X-Forwarded-For       (use the LEFTMOST entry — original client)
    3. request.client.host   (raw socket source — only when no proxy)
  Phase 6.1's Caddyfile / nginx snippets pre-configure (1) + (2)
  so the rate limiter sees real client IPs, not the proxy's
  loopback.

ENDPOINTS GATED:
  POST /login         — login attempt (highest abuse value)
  GET/POST /api/*     — all API routes EXCEPT /api/health
                        (health endpoints must never rate-limit)

WHAT'S DELIBERATELY OUT OF SCOPE:
  * Distributed rate limiting (Redis / Memcache) — single web
    process is the deployment shape we ship. Future Phase 6.x
    point release if multi-instance becomes a thing.
  * IP-blocklist UI — operators can edit a static blocklist file
    today; a UI lands in Phase 8 if asked.
  * Per-route weights — uniform 1-token cost for now. If /api/chat
    starts costing real money, we can give it a heavier weight.

P5 (plain-text state): the limiter is purely in-memory. Restart
clears all buckets. That's intentional — a brief lull after
restart isn't a meaningful protection gap, and persistence would
mean another file to back up.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field


# ---------- Defaults (overridable via env) ----------

DEFAULT_BURST = int(os.environ.get("JANUS_RATE_LIMIT_BURST", "60"))
DEFAULT_REFILL_PER_SECOND = float(
    os.environ.get("JANUS_RATE_LIMIT_REFILL_PER_SECOND", "1.0")
)


@dataclass
class _Bucket:
    """Per-IP token bucket state."""

    tokens: float
    last_refill: float


@dataclass
class TokenBucketLimiter:
    """In-memory token-bucket rate limiter. Thread-safe.

    Attributes:
      capacity:           Max tokens (= burst size).
      refill_per_second:  Tokens added per real-time second.
    """

    capacity: int = DEFAULT_BURST
    refill_per_second: float = DEFAULT_REFILL_PER_SECOND
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def consume(self, key: str, *, cost: float = 1.0, now: float | None = None) -> tuple[bool, float]:
        """Attempt to consume `cost` tokens for `key`. Returns
        (allowed, retry_after_seconds). retry_after_seconds is 0 when
        allowed; otherwise the time until enough tokens have
        refilled to satisfy the request.
        """
        ts = now if now is not None else time.time()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(self.capacity), last_refill=ts)
                self._buckets[key] = bucket
            # Refill based on elapsed time.
            elapsed = max(0.0, ts - bucket.last_refill)
            bucket.tokens = min(
                float(self.capacity),
                bucket.tokens + elapsed * self.refill_per_second,
            )
            bucket.last_refill = ts
            if bucket.tokens >= cost:
                bucket.tokens -= cost
                return True, 0.0
            # Retry-after is the time until enough tokens refill.
            deficit = cost - bucket.tokens
            retry_after = (
                deficit / self.refill_per_second
                if self.refill_per_second > 0
                else 60.0
            )
            return False, retry_after

    def reset(self, key: str | None = None) -> None:
        """Clear one bucket or all of them. Test-friendly."""
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                self._buckets.pop(key, None)


# ---------- Client IP extraction ----------


def client_ip_from_headers(
    headers: dict[str, str] | None,
    fallback: str | None = None,
) -> str:
    """Pull the real client IP from proxy-set headers, falling back
    to the raw socket source. Returns '' if nothing usable found.

    Phase 6.1's Caddy + nginx snippets set both X-Real-IP and
    X-Forwarded-For so this function returns the actual user IP
    even when the request goes through a reverse proxy."""
    headers = headers or {}
    # Header lookups should be case-insensitive — FastAPI lower-cases
    # incoming header keys, but tests / synthetic headers may not.
    norm = {k.lower(): v for k, v in headers.items()}

    real_ip = (norm.get("x-real-ip") or "").strip()
    if real_ip:
        return real_ip
    fwd = (norm.get("x-forwarded-for") or "").strip()
    if fwd:
        # Leftmost entry is the original client.
        first = fwd.split(",")[0].strip()
        if first:
            return first
    return (fallback or "").strip()


# ---------- Path predicate ----------

# Endpoints exempt from rate limiting. Health endpoints must NEVER
# rate-limit — monitoring tools probe constantly and a 429 there
# would falsely mark the service as degraded.
EXEMPT_PATHS: tuple[str, ...] = (
    "/api/health",
)


def is_rate_limited_path(path: str) -> bool:
    """Return True if the request path should be rate-limited.

    Gated paths: /login + /api/* except EXEMPT_PATHS.
    All other paths (static assets, /, /chat, etc.) are NOT
    rate-limited — those are either auth-gated or unbounded reads.
    """
    if path in EXEMPT_PATHS:
        return False
    if path == "/login":
        return True
    if path.startswith("/api/"):
        # Safe-list any future EXEMPT_PATHS check; the constant
        # tuple is the canonical exemption list.
        return path not in EXEMPT_PATHS
    return False


# ---------- Module-level singleton (web.py imports this) ----------

_default_limiter: TokenBucketLimiter | None = None


def get_default_limiter() -> TokenBucketLimiter:
    """Lazy-construct the process-wide limiter so env-var overrides
    (set in tests via monkeypatch) take effect."""
    global _default_limiter
    if _default_limiter is None:
        _default_limiter = TokenBucketLimiter(
            capacity=int(os.environ.get("JANUS_RATE_LIMIT_BURST", str(DEFAULT_BURST))),
            refill_per_second=float(os.environ.get(
                "JANUS_RATE_LIMIT_REFILL_PER_SECOND",
                str(DEFAULT_REFILL_PER_SECOND),
            )),
        )
    return _default_limiter


def reset_default_limiter() -> None:
    """Test helper — clear the singleton so new env values apply."""
    global _default_limiter
    _default_limiter = None
