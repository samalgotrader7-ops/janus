"""Tests for Phase 10 — MCP client.

Uses a `FakeTransport` to simulate the JSON-RPC subprocess so we cover
client behavior without spawning a real MCP server.
"""
from __future__ import annotations
import json

import pytest

from janus import config
from janus.mcp.client import (
    McpClient, McpServerConfig, McpTool, Transport,
    load_servers, mount_mcp_tools,
    register_client, unregister_client, get_active_clients,
)
from janus.tools import default_registry
from janus.tools.base import Registry


class FakeTransport(Transport):
    """Scripts canned responses for the McpClient."""

    def __init__(self):
        self.sent: list[dict] = []
        self._responses: list[dict] = []
        self._closed = False

    def queue(self, msg: dict) -> None:
        self._responses.append(msg)

    def send(self, msg: dict) -> None:
        self.sent.append(msg)

    def recv(self, timeout=None) -> dict:
        if not self._responses:
            raise RuntimeError("FakeTransport: no queued response")
        return self._responses.pop(0)

    def close(self) -> None:
        self._closed = True


def _approve(*a, **kw):
    return True


def _deny(*a, **kw):
    return False


# ---------- load_servers ----------


def test_load_servers_from_janus_native(janus_home):
    config.MCP_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.MCP_SERVERS_FILE.write_text(json.dumps({
        "mcpServers": {
            "fs": {"command": "node", "args": ["fs-mcp.js"]},
        }
    }), encoding="utf-8")
    servers = load_servers()
    assert "fs" in servers
    assert servers["fs"].command == "node"
    assert servers["fs"].args == ["fs-mcp.js"]


def test_load_servers_from_claude_settings_when_janus_missing(
    janus_home, monkeypatch, tmp_path,
):
    """Interop: read ~/.claude/settings.json if no Janus config present."""
    fake_claude = tmp_path / "claude_settings.json"
    fake_claude.write_text(json.dumps({
        "mcpServers": {
            "github": {"command": "npx", "args": ["-y", "gh-mcp"], "env": {"X": "1"}},
        }
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CLAUDE_SETTINGS_FILE", fake_claude)
    # Janus file doesn't exist.
    servers = load_servers()
    assert "github" in servers
    assert servers["github"].args == ["-y", "gh-mcp"]
    assert servers["github"].env == {"X": "1"}


def test_load_servers_janus_takes_precedence_over_claude(
    janus_home, monkeypatch, tmp_path,
):
    config.MCP_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.MCP_SERVERS_FILE.write_text(json.dumps({
        "mcpServers": {"x": {"command": "janus-cmd"}}
    }), encoding="utf-8")
    fake_claude = tmp_path / "claude_settings.json"
    fake_claude.write_text(json.dumps({
        "mcpServers": {"x": {"command": "claude-cmd"}}
    }), encoding="utf-8")
    monkeypatch.setattr(config, "CLAUDE_SETTINGS_FILE", fake_claude)
    servers = load_servers()
    assert servers["x"].command == "janus-cmd"


def test_load_servers_no_config_returns_empty(janus_home, monkeypatch, tmp_path):
    monkeypatch.setattr(
        config, "CLAUDE_SETTINGS_FILE", tmp_path / "definitely-missing.json",
    )
    assert load_servers() == {}


def test_load_servers_skips_disabled(janus_home):
    config.MCP_SERVERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    config.MCP_SERVERS_FILE.write_text(json.dumps({
        "mcpServers": {
            "off": {"command": "x", "disabled": True},
            "on":  {"command": "y"},
        }
    }), encoding="utf-8")
    servers = load_servers()
    assert servers["off"].enabled is False
    assert servers["on"].enabled is True


# ---------- McpClient ----------


def test_mcp_client_initialize_sends_handshake(janus_home):
    t = FakeTransport()
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "x"}})
    c = McpClient(t, "test")
    c.initialize()
    assert t.sent[0]["method"] == "initialize"
    assert t.sent[0]["id"] == 1
    # The 'initialized' notification follows.
    assert any(m.get("method") == "notifications/initialized" for m in t.sent)


def test_mcp_client_list_tools_returns_definitions(janus_home):
    t = FakeTransport()
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {
        "tools": [
            {"name": "read_file", "description": "Read a file",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}}},
            {"name": "list_dir", "description": "List a dir"},
        ]
    }})
    c = McpClient(t, "test")
    tools = c.list_tools()
    assert [t["name"] for t in tools] == ["read_file", "list_dir"]


def test_mcp_client_call_tool_returns_text_content(janus_home):
    t = FakeTransport()
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {
        "content": [{"type": "text", "text": "hello world"}],
    }})
    c = McpClient(t, "test")
    result = c.call_tool("echo", {"msg": "x"})
    assert result == "hello world"


def test_mcp_client_call_tool_surfaces_error(janus_home):
    t = FakeTransport()
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {
        "isError": True, "content": "bad request",
    }})
    c = McpClient(t, "test")
    out = c.call_tool("broken", {})
    assert "error: MCP tool reported error" in out


def test_mcp_client_drains_unrelated_notifications(janus_home):
    t = FakeTransport()
    # Notification comes first (no id), then the matching response.
    t.queue({"jsonrpc": "2.0", "method": "notifications/log", "params": {"msg": "hi"}})
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    c = McpClient(t, "test")
    assert c.list_tools() == []


# ---------- McpTool wrapping + registry mounting ----------


def test_mcp_tool_uses_namespaced_capability_and_dangerous_flag(janus_home):
    t = FakeTransport()
    c = McpClient(t, "fs")
    tool = McpTool(
        "fs", "read_file",
        {"description": "Read a file",
         "inputSchema": {"type": "object", "properties": {}}},
        c,
    )
    assert tool.name == "mcp_fs_read_file"
    assert tool.dangerous is True
    # Approve path is exercised in test_mcp_tool_blocks_on_denial.
    captured = {}
    def cap_approver(label, details, **kw):
        captured["cap"] = kw.get("capability")
        return False
    out = tool.run({"path": "x"}, cap_approver)
    assert captured["cap"] == ("mcp", "fs", "read_file")
    assert out.startswith("refused by user")


def test_mount_mcp_tools_into_registry(janus_home):
    t = FakeTransport()
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {
        "tools": [
            {"name": "read_file"},
            {"name": "write_file"},
        ]
    }})
    c = McpClient(t, "fs")
    reg = Registry([])
    n = mount_mcp_tools(reg, "fs", c)
    assert n == 2
    assert "mcp_fs_read_file" in reg.names()
    assert "mcp_fs_write_file" in reg.names()


def test_default_registry_picks_up_active_mcp_clients(janus_home):
    t = FakeTransport()
    t.queue({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "t1"}]}})
    c = McpClient(t, "demo")
    register_client("demo", c)
    try:
        reg = default_registry()
        assert "mcp_demo_t1" in reg.names()
    finally:
        unregister_client("demo")
    # After unregister, a fresh registry no longer mounts those tools.
    # (No new response queued; if it still tried to list, the FakeTransport
    # would raise.)
    t2 = FakeTransport()
    t2.queue({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    register_client("other", McpClient(t2, "other"))
    try:
        reg2 = default_registry()
        assert "mcp_demo_t1" not in reg2.names()
    finally:
        unregister_client("other")


def test_register_unregister_lifecycle(janus_home):
    t = FakeTransport()
    c = McpClient(t, "x")
    register_client("x", c)
    assert "x" in get_active_clients()
    assert unregister_client("x") is True
    assert "x" not in get_active_clients()
    assert unregister_client("x") is False  # idempotent
