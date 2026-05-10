"""
user_scope.py — per-user state-path resolver (v1.33.4, Phase 6.5).

WHY THIS EXISTS:
Phase 6 / Production hardening. Janus's default is single-user
(``JANUS_SINGLE_USER=1``, set since v1.25.2): one operator on a
personal VPS / laptop. For a small-team deployment, different
users want isolated memory + conversations + skills without
seeing each other's state.

This module ships the FRAMEWORK for per-user state:
  * ``current_user_id()`` — resolves the active user identity
  * ``user_home(user_id)`` — returns the right Path for state
  * ``is_multi_user()`` — flag check

It does NOT yet rewrite every consumer. Memory, web sessions, and
conversations adopt this incrementally over future point releases.
The single-user default keeps existing setups working unchanged.

ENABLING MULTI-USER:
  export JANUS_SINGLE_USER=0
  # Each authenticated user gets ~/.janus/users/<id>/ with their
  # own memory, conversations, etc.

USER IDENTITY:
  * Web: the login session carries a username; surfaced via
    request headers / state. Default: 'default' when unauth'd.
  * cli_rich / cli: always 'default' (one shell = one user).
  * Telegram: chat_id is the user identifier (each chat is its
    own conversation context).

DESIGN NOTES:
  * Empty / invalid user_id falls back to 'default' so missing
    identity doesn't error out — it just lands in the shared
    state.
  * User IDs are sanitized: filesystem-safe characters only
    ([a-z0-9_-]), max 64 chars. Reject path-traversal attempts.
  * Single-user mode: user_home(anything) === HOME. The multi-
    user path doesn't exist and isn't created.

P5 (plain-text state): user dirs are plain ~/.janus/users/<id>/
with the same internal layout (memory/, conversations/, skills/,
etc.) — operators can ls them.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import config


# Filesystem-safe user id pattern. Lowercase letters, digits,
# underscore, dash. 1-64 chars. Anything else → 'default'.
_VALID_USER_ID = re.compile(r"^[a-z0-9_\-]{1,64}$")

# Default user when not in multi-user mode OR when no identity
# could be resolved.
DEFAULT_USER_ID = "default"


def is_multi_user() -> bool:
    """True when JANUS_SINGLE_USER=0 is set in the env. Defaults
    to False (single-user)."""
    # config.MEMORY_SINGLE_USER is the existing flag — same env
    # var, opposite polarity. We invert to match the semantic
    # 'is multi-user mode active'.
    return not bool(getattr(config, "MEMORY_SINGLE_USER", True))


def sanitize_user_id(raw: str | None) -> str:
    """Return a filesystem-safe user id, or DEFAULT_USER_ID.

    Lowercases input. Strips whitespace. Rejects:
      * empty / None
      * path-traversal patterns ('..', '/', '\\')
      * characters outside [a-z0-9_-]
      * length > 64
    """
    if not raw:
        return DEFAULT_USER_ID
    candidate = str(raw).strip().lower()
    if not candidate:
        return DEFAULT_USER_ID
    if ".." in candidate or "/" in candidate or "\\" in candidate:
        return DEFAULT_USER_ID
    if not _VALID_USER_ID.match(candidate):
        return DEFAULT_USER_ID
    return candidate


def user_home(user_id: str | None = None) -> Path:
    """Return the state-dir Path for `user_id`.

    Single-user mode (default): always returns config.HOME, ignoring
    user_id. Multi-user mode: returns config.HOME/users/<sanitized>.

    Does NOT create the directory — callers that write should mkdir
    it first. This keeps the function pure and read-friendly.
    """
    if not is_multi_user():
        return Path(config.HOME)
    sanitized = sanitize_user_id(user_id)
    return Path(config.HOME) / "users" / sanitized


def ensure_user_home(user_id: str | None = None) -> Path:
    """Same as user_home() but creates the directory and a minimal
    skeleton (empty memory/, conversations/, skills/) so consumers
    don't have to mkdir each subdir."""
    home = user_home(user_id)
    home.mkdir(parents=True, exist_ok=True)
    if is_multi_user():
        for sub in ("memory", "conversations", "skills"):
            (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def current_user_id_from_request_headers(headers: dict[str, str] | None) -> str:
    """Extract the active user from request headers. Used by the
    web gateway. Header conventions:
      X-Janus-User: <id>     — primary identity header
      X-Forwarded-User: <id> — fallback (set by some auth proxies)

    Returns DEFAULT_USER_ID if no header present or invalid.

    Notes:
      * In single-user mode the header is ignored (every request
        sees HOME).
      * In multi-user mode an unauth'd / missing-header request
        still resolves to 'default' — operators may want to gate
        unauth'd traffic at the proxy in that case.
    """
    if not is_multi_user():
        return DEFAULT_USER_ID
    headers = headers or {}
    norm = {k.lower(): v for k, v in headers.items()}
    raw = (
        norm.get("x-janus-user")
        or norm.get("x-forwarded-user")
        or ""
    )
    return sanitize_user_id(raw)
