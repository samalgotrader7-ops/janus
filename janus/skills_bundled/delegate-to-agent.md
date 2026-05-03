---
name: delegate-to-agent
description: Speak A2A or MCP-as-client to delegate a sub-task to another agent (Claude Code, Codex, Devin).
state: quarantined
capabilities:
  shell.exec:
    - "claude *"
    - "codex *"
    - "devin *"
    - "aider *"
  web.fetch:
    - "http://localhost:*"
    - "http://127.0.0.1:*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running delegate-to-agent.

You delegate a self-contained sub-task to another agent over a
standard protocol — Google's A2A, MCP-as-client (where Janus is the
client and another agent exposes a tool surface), or just CLI
invocation of another coding agent.

Steps:
1. CONFIRM the sub-task is delegable. Bad candidates: tasks needing
   the user's persistent state (memory, skills), tasks tightly coupled
   to the current conversation. Good candidates: well-bounded units
   with clear success criteria.
2. PICK the agent. Each has a sweet spot:
   - Claude Code (CLI: `claude -p "..."`) — long-context coding
   - Codex CLI — fast, terse coding
   - Aider — git-integrated changes with diffs
   - Devin — async, browser + shell, longer-horizon
   - A custom MCP-exposed agent — domain-specific
3. BRIEF the delegate well. The other agent has zero context from
   this conversation. Provide: goal, scope, definition of done, any
   constraints (no destructive ops, no network, etc.). Pass the
   brief as a single self-contained prompt.
4. EXECUTE in an isolated workspace (git worktree, or a temp dir)
   so the delegate can't trip over your working tree. Capture stdout
   to a file.
5. REVIEW the result. Don't just paste it back to the user — read it,
   verify it solves the brief, surface anything ambiguous. Delegated
   work needs the same scrutiny as your own.

Never delegate destructive operations (production deploys, force-push,
dropping data). Never delegate work that requires the user's secrets
without confirming the destination handles them. Tear down isolated
workspaces after.
