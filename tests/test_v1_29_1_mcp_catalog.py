"""Tests for v1.29.1 — MCP catalog browser (Phase 4).

v1.29.1 extends ``/mcp`` with three browse subcommands:
  catalog              — full directory of configured + connected servers
  tools <server>       — focused per-server tool list
  inspect <server> <t> — full inputSchema for one tool

Plus two web API endpoints:
  GET /api/mcp/catalog
  GET /api/mcp/inspect?server=<>&tool=<>
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from janus import config
    home = tmp_path / "home"
    home.mkdir()
    mcp_dir = home / "mcp"
    mcp_dir.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "MCP_DIR", mcp_dir)
    monkeypatch.setattr(
        config, "MCP_SERVERS_FILE", mcp_dir / "servers.json",
    )
    # Point CLAUDE_SETTINGS_FILE somewhere harmless so it doesn't
    # accidentally load the real ~/.claude/settings.json
    monkeypatch.setattr(
        config, "CLAUDE_SETTINGS_FILE", home / "_claude_settings.json",
    )
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    # Reset MCP active clients between tests
    from janus.mcp import client as _mcp
    for name in list(_mcp.get_active_clients().keys()):
        _mcp.unregister_client(name)
    return home


def _write_server_config(home: Path, name: str, command: str = "echo") -> None:
    cfg_path = home / "mcp" / "servers.json"
    existing = {}
    if cfg_path.exists():
        existing = json.loads(cfg_path.read_text(encoding="utf-8"))
    existing[name] = {"command": command, "args": ["hello"]}
    cfg_path.write_text(json.dumps(existing), encoding="utf-8")


def _make_fake_client(tools_list: list[dict]):
    """Build a stub McpClient that returns the given tools_list."""
    client = MagicMock()
    client.list_tools.return_value = tools_list
    return client


# ============================================================
# /mcp slash dispatcher routes new subcommands
# ============================================================


def test_dispatcher_routes_catalog():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._cmd_mcp_rich)
    assert '"catalog"' in src
    assert "_cmd_mcp_catalog" in src


def test_dispatcher_routes_tools():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._cmd_mcp_rich)
    assert '"tools"' in src
    assert "_cmd_mcp_tools" in src


def test_dispatcher_routes_inspect():
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._cmd_mcp_rich)
    assert '"inspect"' in src
    assert "_cmd_mcp_inspect" in src


def test_handlers_exist():
    from janus import cli_rich
    assert hasattr(cli_rich, "_cmd_mcp_catalog")
    assert hasattr(cli_rich, "_cmd_mcp_tools")
    assert hasattr(cli_rich, "_cmd_mcp_inspect")


def test_slash_dispatch_help_mentions_new_subs():
    from janus.slash_dispatch import BUILTIN_COMMANDS
    mcp_entry = next(c for c in BUILTIN_COMMANDS if c.name == "/mcp")
    desc = mcp_entry.description.lower()
    assert "catalog" in desc
    assert "tools" in desc
    assert "inspect" in desc


# ============================================================
# /mcp catalog handler
# ============================================================


def test_catalog_renders_configured_only_servers(tmp_path, monkeypatch, capsys):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "stub-srv", command="dummy")
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_catalog(console)
    out = buf.getvalue()
    assert "stub-srv" in out
    assert "configured" in out


def test_catalog_renders_connected_with_tools(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "live-srv", command="dummy")
    from janus.mcp import client as _mcp
    fake = _make_fake_client([
        {
            "name": "search",
            "description": "Search the index",
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
        },
    ])
    _mcp.register_client("live-srv", fake)
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=140, force_terminal=False)
    cli_rich._cmd_mcp_catalog(console)
    out = buf.getvalue()
    assert "live-srv" in out
    assert "search" in out
    assert "Search the index" in out
    # Janus mounted name shown
    assert "mcp_live_srv_search" in out


def test_catalog_handles_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_catalog(console)
    out = buf.getvalue()
    assert "no MCP servers configured" in out


def test_catalog_handles_list_tools_failure(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "broken", command="dummy")
    from janus.mcp import client as _mcp
    bad = MagicMock()
    bad.list_tools.side_effect = RuntimeError("boom")
    _mcp.register_client("broken", bad)
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_catalog(console)
    out = buf.getvalue()
    assert "broken" in out
    assert "list_tools failed" in out or "RuntimeError" in out


# ============================================================
# /mcp tools <server> handler
# ============================================================


def test_tools_unknown_server_warns(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_tools(console, "no-such-srv")
    assert "no MCP server" in buf.getvalue()


def test_tools_configured_but_disconnected_warns(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "configured", command="x")
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_tools(console, "configured")
    out = buf.getvalue()
    assert "configured but not connected" in out


def test_tools_connected_lists_tools(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "live", command="x")
    from janus.mcp import client as _mcp
    fake = _make_fake_client([
        {
            "name": "alpha", "description": "first tool",
            "inputSchema": {"type": "object",
                            "properties": {"x": {}, "y": {}}},
        },
        {
            "name": "beta", "description": "second tool",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ])
    _mcp.register_client("live", fake)
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=140, force_terminal=False)
    cli_rich._cmd_mcp_tools(console, "live")
    out = buf.getvalue()
    assert "alpha" in out
    assert "beta" in out
    assert "first tool" in out
    assert "second tool" in out


def test_tools_handles_empty_tool_list(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "live", command="x")
    from janus.mcp import client as _mcp
    _mcp.register_client("live", _make_fake_client([]))
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_tools(console, "live")
    assert "exposes no tools" in buf.getvalue()


# ============================================================
# /mcp inspect handler
# ============================================================


def test_inspect_unknown_server_warns(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=120, force_terminal=False)
    cli_rich._cmd_mcp_inspect(console, "ghost", "anything")
    assert "not connected" in buf.getvalue()


def test_inspect_unknown_tool_lists_available(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "live", command="x")
    from janus.mcp import client as _mcp
    _mcp.register_client("live", _make_fake_client([
        {"name": "alpha", "description": "x", "inputSchema": {}},
        {"name": "beta", "description": "y", "inputSchema": {}},
    ]))
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=140, force_terminal=False)
    cli_rich._cmd_mcp_inspect(console, "live", "gamma")
    out = buf.getvalue()
    assert "no tool 'gamma'" in out
    assert "alpha" in out and "beta" in out


def test_inspect_renders_full_schema(tmp_path, monkeypatch):
    home = _isolate(tmp_path, monkeypatch)
    _write_server_config(home, "live", command="x")
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "q text"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    }
    from janus.mcp import client as _mcp
    _mcp.register_client("live", _make_fake_client([
        {
            "name": "search",
            "description": "Search the index",
            "inputSchema": schema,
        },
    ]))
    from janus import cli_rich
    from rich.console import Console
    import io
    buf = io.StringIO()
    console = Console(file=buf, width=140, force_terminal=False)
    cli_rich._cmd_mcp_inspect(console, "live", "search")
    out = buf.getvalue()
    assert "search" in out
    assert "Search the index" in out
    # Schema is JSON-formatted
    assert "query" in out
    assert "limit" in out
    # Janus mount name appears
    assert "mcp_live_search" in out


# ============================================================
# Web API: /api/mcp/catalog
# ============================================================


# ---------- Web API tests (use the existing janus_home fixture
# pattern from test_web_auth.py — _build_app + bootstrap token). ----------


_HAS_FASTAPI = True
try:
    from fastapi.testclient import TestClient  # noqa: F401
    from janus.gateways import web as _web_mod  # noqa: F401
except ImportError:
    _HAS_FASTAPI = False


def _authed(client, home: Path) -> str:
    """Login + return the CSRF token (mirrors test_web_auth pattern)."""
    from janus.gateways import web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    token = web_auth.get_or_create_bootstrap_token()
    r = client.post("/login", json={"token": token})
    assert r.status_code == 200, r.text
    return r.json()["csrf_token"]


def _reset_mcp_active() -> None:
    from janus.mcp import client as _mcp
    for name in list(_mcp.get_active_clients().keys()):
        _mcp.unregister_client(name)


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_catalog_requires_auth(janus_home):
    _reset_mcp_active()
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    from janus.gateways import web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    # No login → 401
    resp = c.get("/api/mcp/catalog")
    assert resp.status_code == 401


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_catalog_returns_servers_list(janus_home):
    _reset_mcp_active()
    _write_server_config(janus_home, "stub-srv", command="dummy")
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    app = web_mod._build_app()
    c = TestClient(app)
    _authed(c, janus_home)
    resp = c.get("/api/mcp/catalog")
    assert resp.status_code == 200
    data = resp.json()
    assert "servers" in data
    names = [s["name"] for s in data["servers"]]
    assert "stub-srv" in names
    stub = next(s for s in data["servers"] if s["name"] == "stub-srv")
    assert stub["connected"] is False
    assert stub["tools"] == []


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_catalog_includes_connected_tools(janus_home):
    _reset_mcp_active()
    _write_server_config(janus_home, "live", command="dummy")
    from janus.mcp import client as _mcp
    fake = _make_fake_client([
        {
            "name": "search",
            "description": "Search the index",
            "inputSchema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
            },
        },
    ])
    _mcp.register_client("live", fake)
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    app = web_mod._build_app()
    c = TestClient(app)
    _authed(c, janus_home)
    resp = c.get("/api/mcp/catalog")
    assert resp.status_code == 200
    data = resp.json()
    live = next(s for s in data["servers"] if s["name"] == "live")
    assert live["connected"] is True
    assert len(live["tools"]) == 1
    assert live["tools"][0]["name"] == "search"
    assert live["tools"][0]["param_count"] == 1
    assert live["tools"][0]["janus_name"] == "mcp_live_search"


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_inspect_requires_auth(janus_home):
    _reset_mcp_active()
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    from janus.gateways import web_auth
    web_auth.rate_limit_reset()
    web_auth.reset_login_throttle()
    app = web_mod._build_app()
    c = TestClient(app)
    resp = c.get("/api/mcp/inspect?server=x&tool=y")
    assert resp.status_code == 401


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_inspect_missing_params(janus_home):
    _reset_mcp_active()
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    app = web_mod._build_app()
    c = TestClient(app)
    _authed(c, janus_home)
    resp = c.get("/api/mcp/inspect")
    assert resp.status_code == 200
    assert "error" in resp.json()


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_inspect_unknown_server(janus_home):
    _reset_mcp_active()
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    app = web_mod._build_app()
    c = TestClient(app)
    _authed(c, janus_home)
    resp = c.get("/api/mcp/inspect?server=ghost&tool=anything")
    assert resp.status_code == 200
    assert "not connected" in resp.json().get("error", "")


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_inspect_returns_full_schema(janus_home):
    _reset_mcp_active()
    _write_server_config(janus_home, "live", command="dummy")
    schema = {
        "type": "object",
        "properties": {
            "q": {"type": "string"}, "n": {"type": "integer"},
        },
        "required": ["q"],
    }
    from janus.mcp import client as _mcp
    _mcp.register_client("live", _make_fake_client([
        {"name": "search", "description": "x", "inputSchema": schema},
    ]))
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    app = web_mod._build_app()
    c = TestClient(app)
    _authed(c, janus_home)
    resp = c.get("/api/mcp/inspect?server=live&tool=search")
    assert resp.status_code == 200
    data = resp.json()
    assert data["server"] == "live"
    assert data["tool"] == "search"
    assert data["janus_name"] == "mcp_live_search"
    assert data["input_schema"]["properties"]["q"]["type"] == "string"
    assert data["input_schema"]["required"] == ["q"]


@pytest.mark.skipif(not _HAS_FASTAPI, reason="fastapi not installed")
def test_api_inspect_unknown_tool_returns_available(janus_home):
    _reset_mcp_active()
    _write_server_config(janus_home, "live", command="dummy")
    from janus.mcp import client as _mcp
    _mcp.register_client("live", _make_fake_client([
        {"name": "alpha", "description": "x",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "beta", "description": "y",
         "inputSchema": {"type": "object", "properties": {}}},
    ]))
    from fastapi.testclient import TestClient
    from janus.gateways import web as web_mod
    app = web_mod._build_app()
    c = TestClient(app)
    _authed(c, janus_home)
    resp = c.get("/api/mcp/inspect?server=live&tool=gamma")
    assert resp.status_code == 200
    data = resp.json()
    assert "no tool 'gamma'" in data.get("error", "")
    assert set(data.get("available", [])) == {"alpha", "beta"}
