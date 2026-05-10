# Tutorial 04 — Connect MCP

**Goal**: configure an MCP server, list its tools, connect it,
and call one of its tools through Janus. ~10 minutes.

**Prereq**: [Tutorial 03](03-memory-loop.md) — basic
memory flow.

---

## What MCP is

MCP (Model Context Protocol) is a standard for plugging external
tools into LLM agents. The MCP ecosystem has grown around
Anthropic's spec — Claude Code, Cursor, Codex CLI, and Janus all
speak it.

Janus v1.x supports **stdio transport** — the MCP server runs as
a subprocess and Janus talks to it over stdin/stdout JSON-RPC.
HTTP transport is on the v2 roadmap.

## 1. Pick a server

The official servers Anthropic publishes (one-line install via
`npx`):

| Server | What it does |
|---|---|
| `@modelcontextprotocol/server-filesystem` | Read/write files in a sandboxed dir |
| `@modelcontextprotocol/server-git` | Git operations |
| `@modelcontextprotocol/server-sqlite` | Query a SQLite DB |
| `@modelcontextprotocol/server-fetch` | HTTP fetch with content extraction |
| `@modelcontextprotocol/server-memory` | Knowledge graph memory |

For this tutorial we'll use `server-filesystem`.

## 2. Configure the server

Edit `~/.janus/mcp/servers.json`:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/home/me/projects"
      ],
      "disabled": false
    }
  }
}
```

The server runs `npx` with the args. The path argument is the
sandbox root — the server refuses to read/write outside it.

## 3. List the catalog

```
› /mcp catalog
```

Output:

```
● filesystem  (configured · npx -y @modelcontextprotocol/server-filesystem /home/me/projects)
```

Grey ● = configured but not connected. Yellow ⚠ = config entry
skipped (e.g., a `url`-based HTTP server, which v1.x doesn't
support yet — you'll see "HTTP transport not supported" if you
have one).

## 4. Connect it

```
› /mcp connect filesystem
```

Output:

```
● filesystem  (connected · 12 tool(s))
    read_file (1 param) — Read the contents of a file at the given path
      janus name: mcp_filesystem_read_file
    write_file (2 params) — Write content to a file at the given path
      janus name: mcp_filesystem_write_file
    list_directory (1 param) — List files and subdirectories at the path
      janus name: mcp_filesystem_list_directory
    ...
```

Janus spawned the subprocess, ran the MCP `initialize` handshake,
called `tools/list`, and registered each tool with the
`mcp_<server>_<tool>` naming pattern.

## 5. Call a tool

The model can now call MCP tools the same way it calls built-in
ones:

```
› what's in my projects directory?
```

Janus calls `mcp_filesystem_list_directory` and reports back. MCP
tools are `dangerous=True` by default — the user sees a y/N
prompt per call. Capability tokens in skills can grant
auto-approval:

```yaml
# in a skill's frontmatter:
capabilities:
  mcp.filesystem: ["list_directory", "read_file"]
```

Now `list_directory` and `read_file` auto-allow when this skill
is loaded; `write_file` still prompts.

## 6. Disconnect

```
› /mcp disconnect filesystem
```

Stops the subprocess and removes the tools from the registry. The
config in `~/.janus/mcp/servers.json` is unchanged — `/mcp connect
filesystem` re-spawns it.

## 7. Inspect tools without connecting

To see a single tool's full input schema:

```
› /mcp inspect filesystem read_file
```

Useful for debugging "is the model calling this tool with the
right args?" questions.

## Skill-MCP integration

The most powerful pattern: a skill grants MCP capabilities for
its specific use case. Example skill that uses the git MCP:

```markdown
---
name: pr-review-mcp
description: Review the diff of the current branch using MCP git.
state: quarantined
triggers: ["review with mcp"]
capabilities:
  mcp.git: ["status", "diff_unstaged", "log"]
---

When the user asks for an MCP-backed PR review:
1. Call mcp_git_status to see current state.
2. Call mcp_git_diff_unstaged to read the diff.
3. Call mcp_git_log with --max-count=5 for commit history.
4. Summarize findings.
```

Now you have skills that bring their own tools.

## What's next

You've completed the four-tutorial intro. Next steps:

- **Production deploy**: see [`scripts/install_services.sh`](../scripts/install_services.sh) for the systemd-managed deployment pattern.
- **Trigger automations**: `~/.janus/triggers/*.json` for scheduled
  agents (cron-style). Run `janus daemon` to fire them.
- **Web UI**: `janus web` for a browser-based chat interface with
  cost charts, MCP catalog browser, and persistent grants panel.
- **Telegram gateway**: `janus telegram` for chat from your phone.
