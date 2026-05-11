"""
tests/test_mcp_server.py — coverage for the v1.41.0 Janus MCP server.

Tests run dispatch_message() directly (no subprocess) so they're fast
and don't depend on the python-on-PATH or environment. The smoke test
at scripts/smoke_mcp_server.py exercises the stdio path end-to-end.

Pins:
  * initialize returns the spec'd shape (protocolVersion, serverInfo,
    capabilities.tools).
  * tools/list returns the expected tool names.
  * tools/call returns content + isError shape.
  * Unknown tool → isError=True, doesn't crash.
  * Unknown method → JSON-RPC -32601 error response.
  * notifications/* return None (no response per JSON-RPC 2.0).
"""

from __future__ import annotations

import json
import uuid

import pytest


def test_initialize_response_shape():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    })
    assert resp is not None
    assert resp["jsonrpc"] == "2.0"
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == "2024-11-05"
    assert result["serverInfo"]["name"] == "janus"
    assert "tools" in result["capabilities"]


def test_tools_list_contains_required_tools():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    for required in (
        "janus_agent_list",
        "janus_agent_dispatch",
        "janus_agent_memory_get",
        "janus_agent_memory_set",
        "janus_agent_memory_note",
        "janus_blackboard_get",
        "janus_blackboard_set",
        "janus_blackboard_all",
        "janus_bus_send",
        "janus_bus_recv",
        "janus_a2a_card",
        "janus_a2a_dispatch",
    ):
        assert required in names, f"missing {required}"
    # Every tool must declare description + inputSchema.
    for t in tools:
        assert t["description"], t
        assert isinstance(t["inputSchema"], dict), t


def test_tools_call_agent_list_returns_text():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "janus_agent_list", "arguments": {}},
    })
    result = resp["result"]
    assert result["isError"] is False
    assert "claude" in result["content"][0]["text"]


def test_tools_call_unknown_tool_returns_iserror():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "nope_not_a_tool", "arguments": {}},
    })
    assert resp["result"]["isError"] is True
    assert "unknown tool" in resp["result"]["content"][0]["text"]


def test_unknown_method_returns_rpc_error():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({
        "jsonrpc": "2.0", "id": 5, "method": "totally/unknown",
    })
    assert "error" in resp
    assert resp["error"]["code"] == -32601


def test_notifications_initialized_returns_none():
    """JSON-RPC notifications (no id) must not produce a response."""
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert resp is None


def test_blackboard_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    import importlib
    from janus import config, blackboard
    importlib.reload(config)
    importlib.reload(blackboard)
    from janus.mcp import server as mcp_server
    importlib.reload(mcp_server)

    run_id = f"unit-{uuid.uuid4().hex[:8]}"

    resp = mcp_server.dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "janus_blackboard_set",
            "arguments": {"run_id": run_id, "key": "foo", "value": "bar"},
        },
    })
    assert resp["result"]["isError"] is False

    resp = mcp_server.dispatch_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "janus_blackboard_get",
            "arguments": {"run_id": run_id, "key": "foo"},
        },
    })
    assert resp["result"]["content"][0]["text"] == "bar"
    assert resp["result"]["isError"] is False

    # nested value round-trips through JSON encode/decode
    resp = mcp_server.dispatch_message({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": "janus_blackboard_set",
            "arguments": {
                "run_id": run_id, "key": "nested",
                "value": {"a": 1, "b": [2, 3]},
            },
        },
    })
    assert resp["result"]["isError"] is False
    resp = mcp_server.dispatch_message({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {
            "name": "janus_blackboard_get",
            "arguments": {"run_id": run_id, "key": "nested"},
        },
    })
    got = json.loads(resp["result"]["content"][0]["text"])
    assert got == {"a": 1, "b": [2, 3]}


def test_agent_memory_round_trip_via_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.agents import memory as agent_memory
    importlib.reload(agent_memory)
    from janus.mcp import server as mcp_server
    importlib.reload(mcp_server)

    resp = mcp_server.dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {
            "name": "janus_agent_memory_set",
            "arguments": {"agent": "claude", "key": "ping", "value": "pong"},
        },
    })
    assert resp["result"]["isError"] is False

    resp = mcp_server.dispatch_message({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "janus_agent_memory_get",
            "arguments": {"agent": "claude", "key": "ping"},
        },
    })
    assert resp["result"]["content"][0]["text"] == "pong"


def test_a2a_card_tool_returns_valid_card():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "janus_a2a_card", "arguments": {}},
    })
    card = json.loads(resp["result"]["content"][0]["text"])
    assert card["name"] == "Janus"
    assert isinstance(card.get("skills"), list)
    assert len(card["skills"]) >= 1


def test_tools_call_missing_required_arg_returns_error():
    from janus.mcp.server import dispatch_message
    # janus_agent_dispatch requires name + prompt
    resp = dispatch_message({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "janus_agent_dispatch", "arguments": {}},
    })
    assert resp["result"]["isError"] is True
    assert "required" in resp["result"]["content"][0]["text"]


def test_malformed_envelope_returns_invalid_request():
    from janus.mcp.server import dispatch_message
    resp = dispatch_message("not a dict")
    assert resp["error"]["code"] == -32600
