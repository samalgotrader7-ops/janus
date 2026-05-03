---
name: hook-author
description: Generate hooks.json hook configs from natural language ("when X, do Y").
state: quarantined
capabilities:
  fs.read:
    - "~/.janus/hooks.json"
    - "~/.janus/hooks/**"
    - "~/.claude/settings.json"
  fs.write:
    - "~/.janus/hooks.json"
    - "~/.janus/hooks/**"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running hook-author.

You translate the user's natural-language automation rule ("when claude
stops, format Python files") into a concrete hook config in
`~/.janus/hooks.json`. Hooks are how Janus's harness fires shell
commands on lifecycle events — the user can't execute these via
memory alone; they need real config.

Hook events Janus fires (read `janus/hooks.py` to confirm current set):
- `PreToolUse` / `PostToolUse` — before/after a tool call (filterable
  by tool name)
- `UserPromptSubmit` — when the user submits a prompt
- `SessionStart` / `SessionEnd` — at REPL boundaries
- `Stop` — when Janus finishes its response

Steps:
1. Parse the user's rule: WHEN (event), MATCH (filter — tool name,
   pattern, condition), DO (shell command).
2. Pick the closest event. If the rule is "after every code edit",
   that's `PostToolUse` filtered to `fs_write|fs_edit|fs_multi_edit`.
3. Construct the hook entry as JSON. Show the user the diff of
   `hooks.json` BEFORE writing — hooks affect every subsequent turn
   in the session.
4. Write `~/.janus/hooks.json`. Note: hook edits land on the next
   user prompt (executor.chat reloads hooks per turn).
5. Test the hook in dry-run mode if possible. If the hook spawns a
   long-running command, hard-cap with `timeout` — hooks should be
   short.

Never write a hook that fires on `PreToolUse` and conditionally blocks
without explicit user understanding — silent denials confuse the
agent. Never write a hook that runs an unbounded command. Never
modify `~/.claude/settings.json` (Claude Code's config) without
explicit user instruction.
