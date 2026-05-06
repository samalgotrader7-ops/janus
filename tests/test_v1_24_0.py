"""Tests for v1.24.0 — slash dispatcher / cli_rich approval modal /
xterm shell streaming / web files editing."""
from __future__ import annotations

import pytest


# ---------- shared slash dispatcher ----------


def test_slash_dispatch_imports():
    from janus import slash_dispatch
    assert slash_dispatch.SlashCommand is not None
    assert isinstance(slash_dispatch.BUILTIN_COMMANDS, list)
    assert len(slash_dispatch.BUILTIN_COMMANDS) >= 35  # we added a couple


def test_cli_rich_re_exports_slash_metadata():
    """cli_rich.BUILTIN_COMMANDS must be the same object as
    slash_dispatch.BUILTIN_COMMANDS — single source of truth."""
    from janus import cli_rich, slash_dispatch
    assert cli_rich.BUILTIN_COMMANDS is slash_dispatch.BUILTIN_COMMANDS
    assert cli_rich.SlashCommand is slash_dispatch.SlashCommand


def test_slash_lookup_finds_known_commands():
    from janus.slash_dispatch import lookup
    assert lookup("/mode") is not None
    assert lookup("/clear") is not None
    assert lookup("/help") is not None
    assert lookup("/not-a-real-command") is None


def test_slash_registry_register_dispatch():
    from janus.slash_dispatch import SlashRegistry, SlashContext
    reg = SlashRegistry()
    calls = []

    def handler(ctx, arg):
        calls.append((arg,))
        return "handled"

    reg.register("/test", handler)
    assert reg.has("/test")
    handled, result = reg.dispatch("/test foo bar", SlashContext())
    assert handled is True
    assert result == "handled"
    assert calls == [("foo bar",)]


def test_slash_registry_unknown_returns_not_handled():
    from janus.slash_dispatch import SlashRegistry, SlashContext
    reg = SlashRegistry()
    handled, result = reg.dispatch("/nope", SlashContext())
    assert handled is False


def test_slash_registry_handler_exception_is_caught():
    from janus.slash_dispatch import SlashRegistry, SlashContext
    reg = SlashRegistry()
    reg.register("/explode", lambda ctx, arg: 1 / 0)
    msgs = []
    ctx = SlashContext(print_fn=msgs.append)
    handled, result = reg.dispatch("/explode", ctx)
    # Marked handled even on error, so caller doesn't double-handle;
    # error message routed through print_fn.
    assert handled is True
    assert result is None
    assert any("ZeroDivisionError" in m for m in msgs)


def test_split_subcommand():
    from janus.slash_dispatch import split_subcommand
    assert split_subcommand("foo bar baz") == ("foo", "bar baz")
    assert split_subcommand("alone") == ("alone", "")
    assert split_subcommand("") == ("", "")


def test_all_slash_commands_with_customs():
    from janus.slash_dispatch import all_slash_commands

    class _Cmd:
        description = "my custom"

    out = all_slash_commands({"mycmd": _Cmd()})
    names = [c.name for c in out]
    assert "/mycmd" in names
    # Custom commands sort after built-ins.
    custom_idx = names.index("/mycmd")
    builtin_idx = names.index("/help")
    assert custom_idx > builtin_idx


# ---------- cli_rich approval modal ----------


def test_mode_state_session_grants():
    """v1.24.0 added session_grants to ModeState."""
    from janus import permissions
    ms = permissions.ModeState()
    assert ms.session_grants == set()
    key = ("fs_write", "write")
    assert ms.has_grant(key) is False
    ms.grant(key)
    assert ms.has_grant(key) is True
    ms.clear_grants()
    assert ms.has_grant(key) is False


def test_mode_state_grant_is_per_key():
    from janus import permissions
    ms = permissions.ModeState()
    ms.grant(("fs_write", "write"))
    assert ms.has_grant(("fs_write", "write")) is True
    assert ms.has_grant(("fs_write", "exec")) is False
    assert ms.has_grant(("shell", "write")) is False


def test_cli_rich_approver_session_grant_skips_prompt(monkeypatch):
    """If the (tool_name, risk) pair is in session_grants, approver
    returns True WITHOUT calling input(). This is the v1.24 'session'
    behavior — once approved, future calls auto-pass."""
    pytest.importorskip("rich")
    from janus import permissions
    from janus.cli_rich import _make_mode_approver

    ms = permissions.ModeState()
    ms.set("default")
    ms.grant(("fs_write", "write"))

    # Pretend prompt_toolkit / input would explode if called — they
    # shouldn't be called when session grant is active.
    def _boom(*a, **kw):
        raise AssertionError("approver should not have prompted")
    monkeypatch.setattr("builtins.input", _boom)

    # Capture console output without actually rendering Rich panels.
    class _NullConsole:
        def print(self, *a, **kw): pass

    approver = _make_mode_approver(_NullConsole(), ms)
    decision = approver(
        "writing /tmp/x", "details", risk="write", tool_name="fs_write",
    )
    assert decision is True


def test_cli_rich_approver_no_grant_when_no_tool_name(monkeypatch):
    """If tool_name is missing from kwargs, the grant key is empty —
    approver should still prompt (then default to deny here)."""
    pytest.importorskip("rich")
    from janus import permissions
    from janus.cli_rich import _make_mode_approver

    ms = permissions.ModeState()
    ms.set("default")

    monkeypatch.setattr("builtins.input", lambda _: "n")
    try:
        from prompt_toolkit import prompt as _pt_prompt  # noqa: F401
        # If pt is available, approver uses it — patch that too.
        monkeypatch.setattr(
            "prompt_toolkit.prompt", lambda *a, **kw: "n",
        )
    except ImportError:
        pass

    class _NullConsole:
        def print(self, *a, **kw): pass

    approver = _make_mode_approver(_NullConsole(), ms)
    decision = approver(
        "writing", "details", risk="write",
        # no tool_name kwarg
    )
    assert decision is False


# ---------- xterm vendor files shipped ----------


def test_xterm_vendor_files_present():
    from janus.gateways import web as web_mod
    vendor = web_mod.STATIC_DIR / "vendor"
    assert vendor.is_dir(), "v1.24.0 must vendor xterm.js under static/vendor"
    assert (vendor / "xterm.min.js").is_file()
    assert (vendor / "xterm.css").is_file()
    assert (vendor / "xterm-addon-fit.min.js").is_file()


def test_index_html_loads_xterm():
    from janus.gateways import web as web_mod
    html = (web_mod.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    assert "/static/vendor/xterm.min.js" in html
    assert "/static/vendor/xterm-addon-fit.min.js" in html
    assert "/static/vendor/xterm.css" in html


def test_app_js_uses_terminal_api():
    from janus.gateways import web as web_mod
    js = (web_mod.STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert "new Terminal(" in js
    assert "FitAddon" in js
    # Read-only viewer for v1.24.0:
    assert "disableStdin" in js


# ---------- shell stream endpoint ----------


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


def test_shell_stream_requires_auth(janus_home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.get("/api/shells/sh-fake/stream")
    assert r.status_code == 401


def test_shell_stream_unknown_id_returns_404(janus_home):
    c = _authed_client(janus_home)
    r = c.get("/api/shells/sh-totally-fake-id-does-not-exist/stream")
    assert r.status_code == 404


# ---------- web files write endpoint ----------


def test_files_write_requires_auth(janus_home):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod, web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    r = c.post("/api/files/write", json={"path": "x.txt", "content": "x"})
    assert r.status_code == 401


def test_files_write_requires_csrf(janus_home):
    c = _authed_client(janus_home)
    r = c.post("/api/files/write", json={"path": "x.txt", "content": "x"})
    assert r.status_code == 403


def test_files_write_path_required(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/files/write",
        json={"path": "", "content": "x"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400


def test_files_write_content_must_be_string(janus_home):
    c = _authed_client(janus_home)
    r = c.post(
        "/api/files/write",
        json={"path": "a.txt", "content": 12345},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400


def test_files_write_round_trip(janus_home, tmp_path, monkeypatch):
    """Write a file, read it back, confirm content matches."""
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.post(
        "/api/files/write",
        json={"path": "hello.md", "content": "# new file\n"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert (tmp_path / "hello.md").read_text(encoding="utf-8") == "# new file\n"

    # Read endpoint should now see it.
    r2 = c.get("/api/files/read?path=hello.md")
    assert r2.status_code == 200
    assert r2.json()["content"] == "# new file\n"


def test_files_write_overwrites_existing(janus_home, tmp_path, monkeypatch):
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    target = tmp_path / "exists.txt"
    target.write_bytes(b"old content")
    c = _authed_client(janus_home)
    r = c.post(
        "/api/files/write",
        json={"path": "exists.txt", "content": "new content"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 200
    assert target.read_text(encoding="utf-8") == "new content"


def test_files_write_blocks_traversal(janus_home, tmp_path, monkeypatch):
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.post(
        "/api/files/write",
        json={"path": "../escaped.txt", "content": "nope"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400
    assert "outside workspace" in r.json().get("error", "").lower()


def test_files_write_refuses_huge_content(janus_home, tmp_path, monkeypatch):
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    huge = "x" * 1_000_001
    r = c.post(
        "/api/files/write",
        json={"path": "huge.txt", "content": huge},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 413


def test_files_write_refuses_missing_parent(janus_home, tmp_path, monkeypatch):
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    c = _authed_client(janus_home)
    r = c.post(
        "/api/files/write",
        json={"path": "no-such-dir/file.txt", "content": "x"},
        headers={"x-csrf-token": c.csrf_token},
    )
    assert r.status_code == 400
    assert "parent" in r.json().get("error", "").lower()
