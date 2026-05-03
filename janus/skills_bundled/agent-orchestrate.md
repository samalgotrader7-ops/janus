---
name: agent-orchestrate
description: Spawn and coordinate other coding agents (Claude Code, Codex, etc.) for parallel workstreams.
state: quarantined
capabilities:
  shell.exec:
    - "claude *"
    - "codex *"
    - "aider *"
    - "git worktree *"
    - "git status*"
created: 2026-05-03T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running agent-orchestrate.

You delegate units of work to other coding agents (Claude Code, Codex,
Aider) and coordinate their results. Useful for parallel exploration:
N agents try N approaches in N worktrees, then you pick the winner.

Steps:
1. Decide whether parallel work is actually faster than serial. Many
   tasks are NOT parallelizable (refactor with a single hot path,
   bugfix in one function). Don't fan out for no reason.
2. For each parallel unit: create a git worktree
   (`git worktree add ../<branch>-<n> <branch>`) so agents don't
   trip over each other's working trees.
3. Brief each agent the same way: clear task, scope, definition of
   done, time/cost budget. Different agents understand briefs
   differently — Codex prefers terse, Claude Code prefers context.
4. Run agents in parallel via shell. Capture stdout to per-agent log
   files in the worktree so you can read each one's reasoning.
5. Compare outcomes: did they all converge? Is one approach clearly
   better? Surface the diffs side-by-side for the user to pick.
6. Tear down: remove worktrees of approaches the user didn't pick;
   keep the winner.

Don't run more than 3 agents in parallel without confirming with the
user — cost adds up fast. Don't merge any agent's branch without the
user reading the diff. Each agent is unsupervised within its own
worktree; trust but verify.
