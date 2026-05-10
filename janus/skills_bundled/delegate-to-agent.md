---
name: delegate-to-agent
description: Speak A2A or MCP-as-client to delegate a sub-task to another agent (Claude Code, Codex, Aider, Gemini, Devin). Updated v1.38.5 — prefer the first-class wrapper tools over shell.exec.
state: quarantined
capabilities:
  external_cli.claude_code:
    - "exec"
  external_cli.aider:
    - "exec"
  external_cli.codex_cli:
    - "exec"
  external_cli.gemini_cli:
    - "exec"
  shell.exec:
    - "devin *"
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
   - Claude Code (`claude_code` tool) — long-context coding
   - Codex CLI (`codex_cli` tool) — fast, terse coding
   - Aider (`aider` tool) — git-integrated changes with explicit commits
   - Gemini CLI (`gemini_cli` tool) — large-context tasks, --all-files
   - Devin (shell only) — async, browser + shell, longer-horizon
   - A custom MCP-exposed agent — domain-specific
3. BRIEF the delegate well. The other agent has zero context from
   this conversation. Provide: goal, scope, definition of done, any
   constraints (no destructive ops, no network, etc.). Pass the
   brief as a single self-contained prompt.
4. EXECUTE via the first-class wrapper tool (preferred — gives you
   capability-token grants, ANSI strip, output truncation,
   timeout enforcement) OR for Devin via `shell.exec`. Use an
   isolated workspace (git worktree, temp dir) so the delegate
   can't trip over your working tree.
5. REVIEW the result. Don't just paste it back to the user — read it,
   verify it solves the brief, surface anything ambiguous. Delegated
   work needs the same scrutiny as your own.

Never delegate destructive operations (production deploys, force-push,
dropping data). Never delegate work that requires the user's secrets
without confirming the destination handles them. Tear down isolated
workspaces after.
