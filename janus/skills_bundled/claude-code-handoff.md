---
name: claude-code-handoff
description: Hand off a focused coding sub-task to Anthropic's Claude Code via its Print Mode (`claude -p`). Use for long-context coding work where Claude Code's tooling shines.
state: quarantined
project_types:
  - any
capabilities:
  external_cli.claude_code:
    - "exec"
created: 2026-05-10T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running claude-code-handoff.

Use this skill when you want to delegate a focused coding sub-task
to Anthropic's Claude Code CLI, running non-interactively via its
Print Mode. Janus orchestrates; Claude Code does one thing in one
directory and returns its output.

WHEN TO USE:
- Long-context refactors where Claude Code's tooling beats your own.
- Multi-file edits in a directory you can scope tightly via cwd.
- "Does X compile / pass tests" smoke checks where Claude Code can
  read+test in one round-trip.

WHEN NOT TO USE:
- Tasks needing this conversation's memory or prior turn context —
  Claude Code starts cold.
- Production deploys, force-push, anything destructive that you
  wouldn't trust an autonomous agent to do unsupervised.
- Tasks requiring user secrets — Claude Code's session is its own.

THE CLAUDE_CODE TOOL:
You have a `claude_code` tool. Pass:
- prompt: the FULL self-contained brief (Claude Code has no context
  from this conversation — no shared memory, no prior turns).
- cwd: scope tightly. Default workspace is fine for top-level work;
  for narrow tasks, a subdir keeps Claude Code from reading the
  whole repo.
- output_format: "text" (default) for human-readable, "json" when
  you need to parse Claude Code's structured envelope.
- timeout: default 300s. Bump for long tasks; capped at 600s.

PRECONDITIONS:
1. `claude` is on PATH and `claude login` has been run.
2. You've isolated the work — separate cwd, or git worktree, or
   accept that Claude Code may touch arbitrary files in its cwd.
3. The brief is self-contained. Read like a stranger: would another
   coder know what to do without asking questions?

POSTCONDITIONS:
4. Read Claude Code's output. Don't just paste it back. Verify it
   solved the brief, surface anything ambiguous, decide whether
   any follow-up work is needed before reporting "done" to the
   user.

The capability token (`external_cli.claude_code: ["exec"]`) lets
Janus skip the per-call approval prompt while this skill is
active. Without the skill, every claude_code invocation requires
your y/n approval.
