"""
mcp/client.py — Phase 10: minimal MCP client (stdio transport).

WHY:
The MCP ecosystem (Anthropic Model Context Protocol) is the standard
broader Claude Code, Cursor, Codex CLI etc. converged on for connecting
external tools. By speaking it, Janus can absorb every MCP server
without rewriting them. This is the same logic as agentskills.io for
skills — meet the ecosystem where it is.

WHAT'S HERE (v1):
- `Transport` ABC + `StdioTransport` (subprocess + line-delimited JSON-RPC).
- `McpClient` — initialize handshake, tools/list, tools/call.
- `load_servers()` — config from `~/.janus/mcp/servers.json` OR
  `~/.claude/settings.json` (interop).
- `mount_mcp_tools(registry, server_name, client)` — wraps each MCP
  tool as a Janus `Tool` subclass and adds it to the Registry. Tool
  name in the Registry is `mcp_<server>_<tool>` (LLM-tool-call-friendly);
  capability triple is `("mcp", server, tool)` so a skill can grant
  `mcp.<server>: ["<tool>", "<tool2>"]`.

WHAT'S NOT HERE (yet):
- HTTP transport.
- Server lifecycle / reconnection on death.
- Streaming notifications (only request/response).
- Resources, prompts, sampling — only `tools/list` and `tools/call`.

SECURITY:
Every MCP tool is `dangerous=True` by default — they're third-party
code by definition. Capability tokens (`mcp.<server>: [<tool>]`) gate
auto-approval; otherwise the user sees y/N per call.
"""

from __future__ import annotations
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..tools.base import Tool, Registry


# ---------- Server config ----------


@dataclass
class McpServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    enabled: bool = True


def load_servers() -> dict[str, McpServerConfig]:
    """Load MCP server configs.

    Precedence:
      1. ~/.janus/mcp/servers.json (Janus-native).
      2. ~/.claude/settings.json (Claude Code interop) — keys read from
         the standard `mcpServers` block.

    Both shapes follow the Claude Code `mcpServers` convention:
      {"mcpServers": {"<name>": {"command": "...", "args": [...], "env": {...}, "disabled": false}}}

    Janus-native dropping the `mcpServers` wrapper is also accepted:
      {"<name>": {"command": "...", ...}}
    """
    out: dict[str, McpServerConfig] = {}
    for source in (config.MCP_SERVERS_FILE, config.CLAUDE_SETTINGS_FILE):
        try:
            if not source.exists():
                continue
            raw = json.loads(source.read_text(encoding="utf-8"))
        except Exception:
            continue
        block = raw.get("mcpServers") if isinstance(raw, dict) else None
        if block is None and isinstance(raw, dict):
            # Janus-native: top-level keys ARE servers if they look like configs.
            if all(isinstance(v, dict) and "command" in v for v in raw.values()):
                block = raw
        if not isinstance(block, dict):
            continue
        for name, spec in block.items():
            if not isinstance(spec, dict) or "command" not in spec:
                continue
            if name in out:
                continue  # earlier source wins
            out[name] = McpServerConfig(
                name=name,
                command=str(spec["command"]),
                args=[str(a) for a in (spec.get("args") or [])],
                env={str(k): str(v) for k, v in (spec.get("env") or {}).items()},
                enabled=not bool(spec.get("disabled")),
            )
    return out


# ---------- Transport ----------


class Transport:
    """Abstract JSON-RPC line-delimited transport."""

    def send(self, msg: dict) -> None:
        raise NotImplementedError

    def recv(self, timeout: float | None = None) -> dict:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError


class StdioTransport(Transport):
    """Subprocess + stdin/stdout JSON-RPC transport.

    Each direction is line-delimited JSON: one JSON object per line.
    """

    def __init__(self, command: str, args: list[str], env: dict[str, str] | None = None):
        merged_env = dict(os.environ)
        if env:
            merged_env.update(env)
        self._proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=merged_env,
            bufsize=1,
        )

    def send(self, msg: dict) -> None:
        if not self._proc.stdin:
            raise RuntimeError("transport stdin closed")
        self._proc.stdin.write(json.dumps(msg) + "\n")
        self._proc.stdin.flush()

    def recv(self, timeout: float | None = None) -> dict:
        # Use a thread to enforce timeout on a blocking readline.
        if not self._proc.stdout:
            raise RuntimeError("transport stdout closed")
        result: list[str | Exception] = []

        def _read():
            try:
                result.append(self._proc.stdout.readline())
            except Exception as e:
                result.append(e)

        t = threading.Thread(target=_read, daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            raise TimeoutError(f"recv timed out after {timeout}s")
        if not result:
            raise RuntimeError("no data from MCP server")
        v = result[0]
        if isinstance(v, Exception):
            raise v
        line = v.strip()
        if not line:
            raise RuntimeError("MCP server closed stream")
        return json.loads(line)

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


# ---------- Client ----------


class McpClient:
    """Minimal JSON-RPC 2.0 MCP client.

    Implements `initialize`, `tools/list`, `tools/call`. Caller-supplied
    `Transport` lets tests swap in a fake without spawning a process.
    """

    def __init__(self, transport: Transport, server_name: str = ""):
        self._t = transport
        self._next_id = 0
        self._server_name = server_name
        self._initialized = False

    def initialize(self) -> dict:
        resp = self._call(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "janus", "version": "0.10"},
            },
            timeout=config.MCP_INIT_TIMEOUT,
        )
        # Per spec, send 'notifications/initialized' (no response expected).
        try:
            self._t.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        except Exception:
            pass
        self._initialized = True
        return resp

    def list_tools(self) -> list[dict]:
        resp = self._call("tools/list", {}, timeout=config.MCP_INIT_TIMEOUT)
        tools = resp.get("tools") if isinstance(resp, dict) else None
        return tools if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict) -> str:
        resp = self._call(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=config.MCP_CALL_TIMEOUT,
        )
        if not isinstance(resp, dict):
            return f"error: malformed MCP response: {resp!r}"
        if resp.get("isError"):
            return f"error: MCP tool reported error: {resp.get('content')}"
        # MCP content is a list of {type:'text', text:...} blocks.
        content = resp.get("content") or []
        if not isinstance(content, list):
            return str(content)
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, dict):
                parts.append(json.dumps(block))
        return "\n".join(parts) if parts else ""

    def close(self) -> None:
        self._t.close()

    # ---- internals ----

    def _call(self, method: str, params: dict, *, timeout: int) -> Any:
        self._next_id += 1
        rid = self._next_id
        msg = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        self._t.send(msg)
        # Read until we see the matching id (drain notifications in between).
        while True:
            resp = self._t.recv(timeout=timeout)
            if not isinstance(resp, dict):
                raise RuntimeError(f"bad MCP frame: {resp!r}")
            if "id" not in resp:
                # notification — drop and keep waiting
                continue
            if resp["id"] != rid:
                # out-of-order id; drop and keep waiting
                continue
            if "error" in resp:
                err = resp["error"]
                raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
            return resp.get("result")


# ---------- Tool wrapping ----------


class McpTool(Tool):
    """A Janus Tool wrapping one MCP-server tool.

    Capability triple is `("mcp", server, tool)`. Skills grant via
    `mcp.<server>: ["<tool>"]` in their frontmatter.
    """

    def __init__(self, server: str, mcp_name: str, mcp_def: dict, client: McpClient):
        self._server = server
        self._mcp_name = mcp_name
        # Janus tool name: mcp_<server>_<tool>; LLM-tool-call-friendly.
        self.name = f"mcp_{server}_{mcp_name}".replace("-", "_")
        self.description = (
            f"[mcp:{server}] {mcp_def.get('description', '')}".strip()
        )
        # Use the tool's input schema verbatim, defaulting to an empty object.
        self.parameters = mcp_def.get("inputSchema") or {
            "type": "object", "properties": {},
        }
        self.dangerous = True  # MCP tools are third-party by definition
        self._client = client

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        details = (
            f"mcp call: {self._server}/{self._mcp_name}\n"
            f"  args: {json.dumps(args)[:300]}"
        )
        if not approver(
            f"mcp_call: {self._server}/{self._mcp_name}",
            details,
            capability=("mcp", self._server, self._mcp_name),
        ):
            return f"refused by user: {self._server}/{self._mcp_name}"
        try:
            return self._client.call_tool(self._mcp_name, args)
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"


def mount_mcp_tools(registry: Registry, server_name: str, client: McpClient) -> int:
    """Register each tool exposed by `client` into `registry`.
    Returns the number of tools mounted."""
    tools = client.list_tools()
    n = 0
    for tdef in tools:
        if not isinstance(tdef, dict) or "name" not in tdef:
            continue
        wrapped = McpTool(server_name, str(tdef["name"]), tdef, client)
        registry.add_tool(wrapped)
        n += 1
    return n


# ---------- Process-wide client registry ----------


_ACTIVE_CLIENTS: dict[str, "McpClient"] = {}


def get_active_clients() -> dict[str, "McpClient"]:
    """Return the live MCP clients connected this CLI session."""
    return _ACTIVE_CLIENTS


def register_client(name: str, client: "McpClient") -> None:
    if name in _ACTIVE_CLIENTS:
        try:
            _ACTIVE_CLIENTS[name].close()
        except Exception:
            pass
    _ACTIVE_CLIENTS[name] = client


def unregister_client(name: str) -> bool:
    """Disconnect and forget. Returns True if a client was found."""
    client = _ACTIVE_CLIENTS.pop(name, None)
    if client is None:
        return False
    try:
        client.close()
    except Exception:
        pass
    return True


def connect_server(server: McpServerConfig) -> "McpClient":
    """Spawn a server and run its initialization handshake.

    Returns the connected McpClient. Raises on connection failure.
    The client is NOT registered into the active set; callers should call
    register_client() once they're confident the connection is good.
    """
    transport = StdioTransport(server.command, server.args, server.env)
    client = McpClient(transport, server_name=server.name)
    try:
        client.initialize()
    except Exception:
        try:
            client.close()
        except Exception:
            pass
        raise
    return client
