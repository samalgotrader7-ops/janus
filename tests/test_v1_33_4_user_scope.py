"""Tests for v1.33.4 — per-user state-path framework (Phase 6.5).

WHAT THIS SHIPS:
A NEW MODULE (janus/user_scope.py) that resolves per-user state
paths. Single-user mode (default): every call returns config.HOME.
Multi-user mode (JANUS_SINGLE_USER=0): per-user
~/.janus/users/<id>/ paths.

This release is the FRAMEWORK only. Consumer migration (memory,
web sessions, conversations) lands in future point releases. The
single-user default keeps existing setups working unchanged.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from janus import user_scope


# -------------------- sanitize_user_id --------------------


@pytest.mark.parametrize("raw,expected", [
    ("alice", "alice"),
    ("bob_smith", "bob_smith"),
    ("user-1", "user-1"),
    ("ALICE", "alice"),  # lowercased
    ("  alice  ", "alice"),  # stripped
])
def test_sanitize_valid_ids(raw, expected):
    assert user_scope.sanitize_user_id(raw) == expected


@pytest.mark.parametrize("raw", [
    None,
    "",
    "   ",
    "../../../etc/passwd",  # path traversal
    "alice/bob",            # slash
    "alice\\bob",           # backslash
    "alice..bob",           # double-dot
    "user@example.com",     # @ not in allowed set
    "user.name",            # dot not in allowed set
    "user with spaces",
    "x" * 65,               # too long
])
def test_sanitize_rejects_invalid(raw):
    assert user_scope.sanitize_user_id(raw) == user_scope.DEFAULT_USER_ID


def test_sanitize_max_length_64():
    """64 chars is allowed; 65 is not."""
    ok = "a" * 64
    too_long = "a" * 65
    assert user_scope.sanitize_user_id(ok) == ok
    assert user_scope.sanitize_user_id(too_long) == user_scope.DEFAULT_USER_ID


# -------------------- is_multi_user --------------------


def test_is_multi_user_default_false(monkeypatch):
    monkeypatch.delenv("JANUS_SINGLE_USER", raising=False)
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    assert user_scope.is_multi_user() is False


def test_is_multi_user_when_env_zero(monkeypatch):
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    assert user_scope.is_multi_user() is True
    # Restore default for downstream tests.
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


# -------------------- user_home --------------------


def test_user_home_returns_config_home_in_single_user(monkeypatch, tmp_path):
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", tmp_path / ".janus")
    importlib.reload(user_scope)
    monkeypatch.setattr(user_scope.config, "HOME", tmp_path / ".janus")
    home = user_scope.user_home("alice")
    # Single-user: ignores user_id, returns HOME
    assert home == tmp_path / ".janus"


def test_user_home_returns_per_user_in_multi_user(monkeypatch, tmp_path):
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", tmp_path / ".janus")
    importlib.reload(user_scope)
    monkeypatch.setattr(user_scope.config, "HOME", tmp_path / ".janus")
    home_alice = user_scope.user_home("alice")
    home_bob = user_scope.user_home("bob")
    assert home_alice == tmp_path / ".janus" / "users" / "alice"
    assert home_bob == tmp_path / ".janus" / "users" / "bob"
    assert home_alice != home_bob
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_user_home_does_not_create_dir(monkeypatch, tmp_path):
    """user_home is a pure path resolver — doesn't touch the FS."""
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", tmp_path / ".janus")
    importlib.reload(user_scope)
    monkeypatch.setattr(user_scope.config, "HOME", tmp_path / ".janus")
    home = user_scope.user_home("alice")
    assert not home.exists()
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_user_home_path_traversal_falls_back_to_default(monkeypatch, tmp_path):
    """A user_id that looks like '../../../etc' must NOT escape
    HOME. Sanitization catches it; we re-verify here."""
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", tmp_path / ".janus")
    importlib.reload(user_scope)
    monkeypatch.setattr(user_scope.config, "HOME", tmp_path / ".janus")
    home = user_scope.user_home("../../../etc")
    assert home == tmp_path / ".janus" / "users" / "default"
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


# -------------------- ensure_user_home --------------------


def test_ensure_user_home_creates_skeleton(monkeypatch, tmp_path):
    """In multi-user mode, ensure_user_home creates the dir +
    standard subdirs."""
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", tmp_path / ".janus")
    importlib.reload(user_scope)
    monkeypatch.setattr(user_scope.config, "HOME", tmp_path / ".janus")
    home = user_scope.ensure_user_home("alice")
    assert home.exists()
    for sub in ("memory", "conversations", "skills"):
        assert (home / sub).is_dir()
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_ensure_user_home_in_single_user_no_subdirs(monkeypatch, tmp_path):
    """Single-user: ensure_user_home returns HOME and creates it
    if missing, but does NOT create memory/conversations/skills
    subdirs (those are config.MEMORY_DIR etc., handled elsewhere)."""
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    fake_home = tmp_path / "fresh"
    from janus import config
    importlib.reload(config)
    monkeypatch.setattr(config, "HOME", fake_home)
    importlib.reload(user_scope)
    monkeypatch.setattr(user_scope.config, "HOME", fake_home)
    home = user_scope.ensure_user_home("alice")
    assert home == fake_home
    assert home.exists()
    # Subdirs NOT created — they're managed elsewhere in single-user.
    assert not (home / "memory").exists()


# -------------------- current_user_id_from_request_headers --------------------


def test_user_id_from_headers_single_user_always_default(monkeypatch):
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    # Even with a valid header, single-user returns 'default'.
    uid = user_scope.current_user_id_from_request_headers(
        {"X-Janus-User": "alice"}
    )
    assert uid == "default"


def test_user_id_from_headers_multi_user_extracts(monkeypatch):
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    uid = user_scope.current_user_id_from_request_headers(
        {"X-Janus-User": "alice"}
    )
    assert uid == "alice"
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_user_id_falls_back_to_x_forwarded_user(monkeypatch):
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    uid = user_scope.current_user_id_from_request_headers(
        {"X-Forwarded-User": "bob"}
    )
    assert uid == "bob"
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_user_id_case_insensitive_header_name(monkeypatch):
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    uid = user_scope.current_user_id_from_request_headers(
        {"x-janus-user": "alice"}
    )
    assert uid == "alice"
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_user_id_no_header_returns_default(monkeypatch):
    monkeypatch.setenv("JANUS_SINGLE_USER", "0")
    from janus import config
    importlib.reload(config)
    importlib.reload(user_scope)
    uid = user_scope.current_user_id_from_request_headers({})
    assert uid == "default"
    monkeypatch.setenv("JANUS_SINGLE_USER", "1")
    importlib.reload(config)
    importlib.reload(user_scope)


def test_default_user_id_constant():
    """Module exports DEFAULT_USER_ID for consumers that need to
    test 'is this user the default?' without hard-coding."""
    assert user_scope.DEFAULT_USER_ID == "default"


# -------------------- Version pin --------------------


def test_version_bumped_to_1_33_4_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 33, 4)
