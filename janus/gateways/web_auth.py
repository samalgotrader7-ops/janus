"""
gateways/web_auth.py — v1.21 web UI authentication primitives.

PROBLEM:
The pre-v1.21 web gateway exposed `/chat`, `/memory`, `/cost`, and
`/home` to the internet with no authentication. Earlier deployments
ran on public VPS hosts — anyone could read full memory dumps, drive
the agent, and burn tokens. `/memory` and `/cost` were ALWAYS open;
pairing-code auth (when enabled) only gated `/chat` and `/home`.

DESIGN:
Two-token model:

  1. Bootstrap token — auto-generated on first `janus web` start, stored
     in ~/.janus/web_token (mode 0600), printed to console once. The
     user logs in by pasting this into the web form.

  2. Signed session cookie — issued on successful token submission.
     HMAC-signed using ~/.janus/web_secret (mode 0600). HttpOnly +
     SameSite=Strict + Secure (when TLS in front). Default 7-day TTL.

CSRF:
A separate CSRF token (HMAC of session ID + nonce) is issued at login
and validated on every non-GET request via X-CSRF-Token header.

RATE LIMITING:
Token-bucket per (session_id, route_class). 30/min for /chat, 120/min
for read endpoints. Returns 429 with Retry-After.

LOGIN THROTTLE:
5 failed attempts from one IP → 15-minute block. Per-IP counter in
~/.janus/web_auth_state.json. Successful login resets the counter.

PLAIN-TEXT (P5):
Tokens, secrets, and audit log are all plain files in ~/.janus/. Sam
can `cat`, `chmod`, `mv`, `git diff` any of them. Not opaque blobs.

ALL FUNCTIONS:
- get_or_create_bootstrap_token() / rotate_bootstrap_token()
- verify_bootstrap_token(provided)
- get_or_create_secret()
- sign_session(sid, expires_at) / verify_session(cookie_value)
- make_csrf_token(sid) / verify_csrf(sid, token)
- RateLimiter (per-bucket token-bucket)
- record_login_attempt(ip, success) / is_ip_blocked(ip)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import config


# ---------- file paths ----------

def _token_path() -> Path:
    return config.HOME / "web_token"


def _secret_path() -> Path:
    return config.HOME / "web_secret"


def _auth_state_path() -> Path:
    return config.HOME / "web_auth_state.json"


# ---------- bootstrap token (the "primary key" for owning the gateway) ----------


def _atomic_write_secret(path: Path, content: str) -> None:
    """Write `content` to `path` with mode 0600 (owner read/write only).

    Uses a temp file + rename so partial writes can't leak credentials
    to disk. On Windows mode 0600 is best-effort — Windows ACLs don't
    map cleanly to POSIX bits, but we still call os.chmod for clarity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _generate_token() -> str:
    """64-char URL-safe base64 token. ~384 bits of entropy."""
    return secrets.token_urlsafe(48)


def get_or_create_bootstrap_token() -> str:
    """Read the bootstrap token. Generate + persist if absent.

    Returns the token string. Caller can print it to console at server
    startup so the user knows what to paste into the login form.
    """
    p = _token_path()
    if p.is_file():
        try:
            tok = p.read_text(encoding="utf-8").strip()
            if tok:
                return tok
        except OSError:
            pass
    tok = _generate_token()
    _atomic_write_secret(p, tok)
    return tok


def rotate_bootstrap_token() -> str:
    """Replace the bootstrap token with a fresh one.

    Existing signed sessions remain valid until their expiration — the
    bootstrap token is only used to create new sessions. This means
    rotating doesn't kick out logged-in users; it just prevents anyone
    holding the old token from creating NEW sessions.
    """
    tok = _generate_token()
    _atomic_write_secret(_token_path(), tok)
    return tok


def verify_bootstrap_token(provided: str) -> bool:
    """Constant-time compare against the on-disk bootstrap token.

    Empty input or missing on-disk token both return False — never
    silently authenticate against an empty string.
    """
    if not provided:
        return False
    actual = get_or_create_bootstrap_token()
    if not actual:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), actual.encode("utf-8"))


# ---------- HMAC secret (for signing session cookies + CSRF tokens) ----------


def get_or_create_secret() -> bytes:
    """Read the HMAC signing key. Generate + persist if absent.

    Stored as base64 in ~/.janus/web_secret (mode 0600). Returns raw
    bytes for hmac.new().
    """
    p = _secret_path()
    if p.is_file():
        try:
            raw = p.read_text(encoding="utf-8").strip()
            if raw:
                # Stored as URL-safe base64 of 32 random bytes.
                import base64
                return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (OSError, ValueError):
            pass
    key = secrets.token_bytes(32)
    import base64
    _atomic_write_secret(p, base64.urlsafe_b64encode(key).decode("ascii").rstrip("="))
    return key


# ---------- signed session cookies ----------

# Cookie format: "<sid>|<expires_unix>|<hmac_hex>"
# - sid: opaque session identifier (UUID)
# - expires_unix: integer seconds-since-epoch when this session expires
# - hmac_hex: HMAC-SHA256(secret, "<sid>|<expires_unix>") as lowercase hex

_COOKIE_NAME = "janus_session"


def cookie_name() -> str:
    return _COOKIE_NAME


def session_ttl_seconds() -> int:
    return int(os.environ.get("JANUS_WEB_SESSION_TTL", str(7 * 86400)))


def sign_session(sid: str, expires_unix: int | None = None) -> str:
    """Return a cookie value for the given session id.

    Caller passes `expires_unix` for explicit control; otherwise we use
    now + session_ttl_seconds().
    """
    if expires_unix is None:
        expires_unix = int(time.time()) + session_ttl_seconds()
    secret = get_or_create_secret()
    payload = f"{sid}|{expires_unix}"
    sig = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}|{sig}"


def verify_session(cookie_value: str) -> str | None:
    """Return the session id if `cookie_value` is well-formed, signed,
    and not expired. Otherwise None.

    Constant-time HMAC comparison guards against timing attacks.
    """
    if not cookie_value or "|" not in cookie_value:
        return None
    parts = cookie_value.split("|")
    if len(parts) != 3:
        return None
    sid, expires_str, sig_hex = parts
    if not sid or not expires_str or not sig_hex:
        return None
    try:
        expires_unix = int(expires_str)
    except ValueError:
        return None
    if expires_unix < int(time.time()):
        return None
    secret = get_or_create_secret()
    payload = f"{sid}|{expires_unix}"
    expected = hmac.new(secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig_hex):
        return None
    return sid


# ---------- CSRF tokens ----------


def make_csrf_token(sid: str) -> str:
    """Return a CSRF token bound to this session id.

    Format: "<nonce_hex>.<hmac_hex>" — both in lowercase hex. The token
    stays valid for the life of the session. Rotation can be added
    later if a use case demands it.
    """
    nonce = secrets.token_hex(16)
    secret = get_or_create_secret()
    sig = hmac.new(
        secret, f"{sid}|{nonce}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return f"{nonce}.{sig}"


def verify_csrf(sid: str, token: str) -> bool:
    if not token or "." not in token:
        return False
    nonce, _, sig_hex = token.partition(".")
    if not nonce or not sig_hex:
        return False
    secret = get_or_create_secret()
    expected = hmac.new(
        secret, f"{sid}|{nonce}".encode("utf-8"), hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, sig_hex)


# ---------- rate limiting (token bucket) ----------


@dataclass
class _Bucket:
    tokens: float = 0.0
    last_refill: float = field(default_factory=time.monotonic)


class RateLimiter:
    """Token-bucket per (key, route_class).

    Each bucket has a capacity (max tokens) and a refill rate. A request
    consumes one token; if no token is available, take() returns
    (False, retry_after_seconds).

    Two route classes by default:
      * "chat"  — 30 req/min  (chat endpoint, expensive)
      * "read"  — 120 req/min (memory/cost/health, cheap)
    """

    DEFAULTS = {
        "chat": (30, 60.0),   # capacity, refill window seconds
        "read": (120, 60.0),
        "auth": (10, 60.0),   # login attempts: stricter than read
    }

    def __init__(self) -> None:
        self._buckets: dict[tuple[str, str], _Bucket] = {}
        self._lock = threading.Lock()

    def _params(self, route_class: str) -> tuple[int, float]:
        return self.DEFAULTS.get(route_class, (60, 60.0))

    def take(self, key: str, route_class: str = "read") -> tuple[bool, float]:
        capacity, window = self._params(route_class)
        rate = capacity / window  # tokens per second
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get((key, route_class))
            if b is None:
                b = _Bucket(tokens=float(capacity), last_refill=now)
                self._buckets[(key, route_class)] = b
            # Refill since last access.
            elapsed = now - b.last_refill
            b.tokens = min(float(capacity), b.tokens + elapsed * rate)
            b.last_refill = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return (True, 0.0)
            # Compute time until 1 full token would be available.
            deficit = 1.0 - b.tokens
            retry_after = deficit / rate if rate > 0 else 60.0
            return (False, retry_after)

    def reset(self, key: str | None = None) -> None:
        """Clear all buckets, or just buckets for a specific key.

        Test-only helper. Production code never calls this.
        """
        with self._lock:
            if key is None:
                self._buckets.clear()
            else:
                for k in list(self._buckets):
                    if k[0] == key:
                        del self._buckets[k]


# Module-level singleton — one limiter per process. Reset is exposed
# for tests; production code never resets.
_limiter = RateLimiter()


def rate_limit_take(key: str, route_class: str = "read") -> tuple[bool, float]:
    return _limiter.take(key, route_class)


def rate_limit_reset(key: str | None = None) -> None:
    _limiter.reset(key)


# ---------- login throttle (per-IP failure tracking) ----------


_LOGIN_THROTTLE_LOCK = threading.Lock()


def _read_auth_state() -> dict:
    p = _auth_state_path()
    if not p.is_file():
        return {"ips": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"ips": {}}


def _write_auth_state(state: dict) -> None:
    p = _auth_state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(p)


def _block_window_seconds() -> int:
    return int(os.environ.get("JANUS_WEB_LOGIN_BLOCK_S", "900"))  # 15 min


def _block_threshold() -> int:
    return int(os.environ.get("JANUS_WEB_LOGIN_THRESHOLD", "5"))


def record_login_attempt(ip: str, success: bool) -> None:
    """Record a login attempt. Successful login clears the IP's history."""
    if not ip:
        ip = "unknown"
    now = int(time.time())
    with _LOGIN_THROTTLE_LOCK:
        state = _read_auth_state()
        ips = state.setdefault("ips", {})
        entry = ips.setdefault(ip, {"failures": [], "blocked_until": 0})
        if success:
            entry["failures"] = []
            entry["blocked_until"] = 0
        else:
            # Drop failure timestamps older than the block window — we
            # only count recent failures toward the threshold.
            cutoff = now - _block_window_seconds()
            entry["failures"] = [t for t in entry["failures"] if t >= cutoff]
            entry["failures"].append(now)
            if len(entry["failures"]) >= _block_threshold():
                entry["blocked_until"] = now + _block_window_seconds()
        _write_auth_state(state)


def is_ip_blocked(ip: str) -> tuple[bool, int]:
    """Return (blocked, seconds_remaining). seconds_remaining=0 means
    not blocked OR block has expired.
    """
    if not ip:
        return (False, 0)
    now = int(time.time())
    with _LOGIN_THROTTLE_LOCK:
        state = _read_auth_state()
        entry = state.get("ips", {}).get(ip)
        if not entry:
            return (False, 0)
        blocked_until = int(entry.get("blocked_until", 0))
        if blocked_until <= now:
            return (False, 0)
        return (True, blocked_until - now)


def reset_login_throttle() -> None:
    """Test helper — clear all per-IP login state."""
    p = _auth_state_path()
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass
