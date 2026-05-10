---
name: aider-refactor
description: Hand off a focused git-tracked refactor to Aider via `aider --message --yes-always`. Use when you want explicit commits per change.
state: quarantined
project_types:
  - any
capabilities:
  external_cli.aider:
    - "exec"
created: 2026-05-10T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running aider-refactor.

Use this skill when you want Aider to perform a focused refactor or
multi-file edit with EXPLICIT git commits per change. Aider's
killer feature is its tight git integration — every edit becomes a
commit you can read, revert, or cherry-pick.

WHEN TO USE:
- Refactors where the diff is the reviewable artifact.
- Multi-file changes where you want a clean commit history.
- Tasks where the cwd is a git repo and you want auto-commits.

WHEN NOT TO USE:
- Non-git workspaces — aider's --yes-always flow needs a repo.
- Speculative work where you DON'T want commits yet.
- Tasks needing this conversation's context.

THE AIDER TOOL:
You have an `aider` tool. Pass:
- prompt: the self-contained instruction.
- cwd: must be inside a git repo for aider's auto-commit workflow.
- files: optional list of paths to focus on. Without this, aider
  explores the repo automatically. Pin files to keep context tight
  on big repos.
- timeout: default 300s, capped at 600s.

PRECONDITIONS:
1. `aider` is on PATH (`pip install aider-chat`).
2. cwd is a git repo with a clean working tree (or you're OK with
   aider committing on top of dirty state).
3. The brief is self-contained — aider has no context from this
   conversation.
4. If you scope via `files`, those files exist relative to cwd.

POSTCONDITIONS:
5. Read aider's stdout — it includes the commits it made.
6. `git log` to verify expected commits landed.
7. If something broke, `git reset --hard <prev>` rewinds.

The capability token (`external_cli.aider: ["exec"]`) skips the
per-call y/n while this skill is active.
