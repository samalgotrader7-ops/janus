"""Tests for v1.24.1 — persistent grants / logs SSE / CodeMirror /
PTY shells / /grants slash command."""
from __future__ import annotations

import os

import pytest


# ---------- persistent grants ----------


def test_persistent_grant_round_trip(janus_home):
    from janus import permissions
    ms = permissions.ModeState()
    key = ("fs_write", "write")
    assert ms.has_grant(key) is False
    ms.grant_persistent(key)
    assert ms.has_grant(key) is True
    # Fresh instance reads the persisted file.
    ms2 = permissions.ModeState()
    assert ms2.has_grant(key) is True
    # Cleanup.
    ms2.clear_persistent()


def test_persistent_grant_file_mode_0600_on_posix(janus_home):
    if os.name != "posix":
        pytest.skip("POSIX-only file modes")
    from janus import permissions
    ms = permissions.ModeState()
    ms.grant_persistent(("fs_edit", "write"))
    p = permissions._grants_file_path()
    assert p.exists()
    mode = p.stat().st_mode & 0o777
    assert mode == 0o600
    ms.clear_persistent()


def test_revoke_persistent(janus_home):
    from janus import permissions
    ms = permissions.ModeState()
    ms.grant_persistent(("fs_write", "write"))
    ms.grant_persistent(("shell", "exec"))
    ms.revoke_persistent(("fs_write", "write"))
    # Fresh load: only shell remains.
    ms2 = permissions.ModeState()
    assert ms2.has_grant(("fs_write", "write")) is False
    assert ms2.has_grant(("shell", "exec")) is True
    ms2.clear_persistent()


def test_clear_persistent_wipes_file(janus_home):
    from janus import permissions
    ms = permissions.ModeState()
    ms.grant_persistent(("a", "write"))
    ms.grant_persistent(("b", "exec"))
    ms.clear_persistent()
    ms2 = permissions.ModeState()
    s, p = ms2.list_grants()
    assert p == set()


def test_session_grant_does_not_persist(janus_home):
    from janus import permissions
    ms = permissions.ModeState()
    ms.grant(("ephemeral", "write"))  # session-only path
    assert ms.has_grant(("ephemeral", "write")) is True
    # Fresh instance doesn't see it.
    ms2 = permissions.ModeState()
    assert ms2.has_grant(("ephemeral", "write")) is False


# ---------- /grants slash command ----------


def test_grants_handler_list_when_empty(janus_home):
    from janus import slash_dispatch
    ctx = slash_dispatch.SlashContext(state={})
    out = slash_dispatch._h_grants(ctx, "list")
    assert "no approval grants" in out.lower()


def test_grants_handler_list_with_persistent(janus_home):
    from janus import permissions, slash_dispatch
    ms = permissions.ModeState()
    ms.grant_persistent(("fs_write", "write"))
    ctx = slash_dispatch.SlashContext(state={"mode_state": ms})
    out = slash_dispatch._h_grants(ctx, "list")
    assert "fs_write" in out
    assert "persistent" in out.lower()
    ms.clear_persistent()


def test_grants_handler_revoke(janus_home):
    from janus import permissions, slash_dispatch
    ms = permissions.ModeState()
    ms.grant_persistent(("fs_write", "write"))
    ms.grant_persistent(("shell", "exec"))
    ctx = slash_dispatch.SlashContext(state={"mode_state": ms})
    out = slash_dispatch._h_grants(ctx, "revoke fs_write")
    assert "1" in out  # revoked count
    # Fresh load.
    ms2 = permissions.ModeState()
    assert ms2.has_grant(("fs_write", "write")) is False
    assert ms2.has_grant(("shell", "exec")) is True
    ms2.clear_persistent()


def test_grants_handler_clear(janus_home):
    from janus import permissions, slash_dispatch
    ms = permissions.ModeState()
    ms.grant_persistent(("a", "write"))
    ctx = slash_dispatch.SlashContext(state={"mode_state": ms})
    out = slash_dispatch._h_grants(ctx, "clear")
    assert "cleared" in out.lower()
    ms2 = permissions.ModeState()
    s, p = ms2.list_grants()
    assert p == set()


def test_grants_in_builtin_commands():
    from janus import slash_dispatch
    names = [c.name for c in slash_dispatch.BUILTIN_COMMANDS]
    assert "/grants" in names


def test_register_shared_handlers_adds_grants():
    from janus.slash_dispatch import SlashRegistry, register_shared_handlers
    reg = SlashRegistry()
    register_shared_handlers(reg)
    assert reg.has("/grants")


# ---------- logs SSE ----------


def _authed_client(janus_home_path=None):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    token = web_auth.get_or_create_bootstrap_token()
    r = c.post("/login", json={"token": token})
    assert r.status_code == 200
    c.csrf_token = r.json()["csrf_token"]  # type: ignore[attr-defined]
    return c


def test_logs_stream_requires_auth(janus_home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/logs/stream")
    assert r.status_code == 401


# ---------- CodeMirror vendor ----------


def test_codemirror_vendored():
    from janus.gateways import web as web_mod
    vendor = web_mod.STATIC_DIR / "vendor"
    assert (vendor / "codemirror.min.js").is_file()
    assert (vendor / "codemirror.min.css").is_file()
    # At least one mode shipped.
    assert (vendor / "mode-python.min.js").is_file()
    assert (vendor / "mode-markdown.min.js").is_file()


def test_index_html_loads_codemirror():
    from janus.gateways import web as web_mod
    html = (web_mod.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/codemirror.min.css" in html
    assert "/static/vendor/codemirror.min.js" in html
    assert "/static/vendor/mode-python.min.js" in html


def test_app_js_uses_codemirror():
    from janus.gateways import web as web_mod
    js = (web_mod.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "CodeMirror.fromTextArea" in js
    assert "_cmModeFor" in js


# ---------- PTY shells ----------


def test_shell_pty_is_supported_matches_platform():
    from janus.tools import shell_pty
    if os.name == "posix":
        assert shell_pty.is_supported() is True
    else:
        assert shell_pty.is_supported() is False


def test_shell_pty_start_refuses_on_windows():
    if os.name == "posix":
        pytest.skip("Windows-only test")
    from janus.tools import shell_pty
    with pytest.raises(NotImplementedError):
        shell_pty.start_pty_shell("echo hi")


def test_shell_pty_write_stdin_unknown_id(janus_home):
    from janus.tools import shell_pty
    if not shell_pty.is_supported():
        with pytest.raises(NotImplementedError):
            shell_pty.write_stdin("sh-fake", "x")
        return
    with pytest.raises(ValueError):
        shell_pty.write_stdin("sh-totally-fake-id", "x")


def test_shell_pty_kill_unknown_returns_false(janus_home):
    from janus.tools import shell_pty
    assert shell_pty.kill_pty_shell("sh-fake") is False


def test_api_shells_run_pty_on_windows_returns_400(janus_home):
    if os.name == "posix":
        pytest.skip("Windows-specific test")
    c = _authed_client(janus_home)
    r = c.post(
        "/api/shells/run",
        json={"command": "echo hi", "pty": True},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400
    assert "pty" in r.json()["error"].lower()


def test_api_shell_stdin_requires_auth(janus_home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/api/shells/sh-fake/stdin", json={"data": "x"})
    assert r.status_code == 401


def test_api_shell_stdin_requires_csrf(janus_home):
    c = _authed_client(janus_home)
    r = c.post("/api/shells/sh-fake/stdin", json={"data": "x"})
    assert r.status_code == 403


def test_api_shell_stdin_unknown_id(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/shells/sh-totally-fake/stdin",
        json={"data": "x"},
        headers={"x-csrf-token": c.csrf_token},
    )
    # POSIX: 400 (no such PTY). Windows: 400 (POSIX required).
    assert r.status_code == 400


def test_api_shell_stdin_data_must_be_string(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/shells/sh-fake/stdin",
        json={"data": 12345},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400


# ---------- shared registry plugged into cli_rich ----------


def test_cli_rich_dispatcher_consults_registry(janus_home, monkeypatch):
    """v1.24.1: /grants flows through slash_dispatch.SlashRegistry
    rather than cli_rich's legacy if/elif chain."""
    pytest.importorskip("rich")
    from janus import cli_rich, permissions
    state = {
        "mode_state": permissions.ModeState(),
        "messages": [], "conv": None, "verbose": False,
        "stream": True, "custom_commands": {}, "quit": False,
        "turn": 0, "output_style": "markdown", "last_user_input": "",
    }

    msgs = []
    class _Console:
        def print(self, *args, **kw):
            for a in args:
                msgs.append(str(a))

    handled = cli_rich._dispatch(_Console(), "/grants list", state)
    assert handled is True
    assert "_shared_slash_registry" in state
    # Output should mention "no approval grants" or persistent listing.
    blob = "\n".join(msgs).lower()
    assert "approval grant" in blob or "persistent" in blob
