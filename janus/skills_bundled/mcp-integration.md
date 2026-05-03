---
name: mcp-integration
description: Discover, configure, and connect MCP servers — extend Janus's tool surface live.
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/mcp/**"
    - "~/.claude/settings.json"
    - "**"
  fs.write:
    - "~/.janus/mcp/**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running mcp-integration.

You help the user wire MCP (Model Context Protocol) servers into Janus.
Each server exposes additional tools; once connected, they appear as
mcp_<server>_<tool> in the registry.

Steps:
1. List configured servers: read `~/.janus/mcp/servers.json` and
   `~/.claude/settings.json` (Janus interops with Claude's MCP config).
   Report which are configured, which are connected.
2. For NEW SERVER: confirm command, args, env vars. Validate the JSON
   shape before writing. Recommend storing secrets via env vars, not
   inline in the JSON.
3. For CONNECT: instruct the user to run `/mcp connect <name>` — Janus
   spawns the server, lists its tools, mounts them as
   `mcp_<server>_<tool>`. You don't connect on the user's behalf;
   that command is theirs.
4. For DEBUG: a server fails to connect → check the command path, the
   env vars, run the command directly outside Janus to confirm it
   speaks MCP at all.
5. For DISCOVER: list known MCP servers (filesystem, git, gh, brave,
   slack, sentry, …). Don't recommend installing servers without
   confirming the user wants the trust surface.

Never modify `~/.claude/settings.json` without explicit user
confirmation — that file is shared with Claude Code. Don't paste
secrets into `mcp/servers.json`; route through env vars.
