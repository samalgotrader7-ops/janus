---
name: gemini-cli-task
description: Hand off a focused coding sub-task to Google's Gemini CLI via `gemini -p`. Useful when Gemini's large-context model is the right pick.
state: quarantined
project_types:
  - any
capabilities:
  external_cli.gemini_cli:
    - "exec"
created: 2026-05-10T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running gemini-cli-task.

Use this skill to delegate a focused coding sub-task to Google's
open-source Gemini CLI (https://github.com/google-gemini/gemini-cli)
running non-interactively via `gemini -p`.

WHEN TO USE:
- Tasks where Gemini's large-context model is the right fit.
- Workspace-wide tasks via `extra_args=["--all-files"]`.
- Sandboxed exploration via `extra_args=["--sandbox"]`.
- Specific model pinning via `extra_args=["--model", "gemini-2.5-pro"]`.

WHEN NOT TO USE:
- Tasks requiring this conversation's context.
- Speculative work where you can't tolerate file edits.

THE GEMINI_CLI TOOL:
You have a `gemini_cli` tool. Pass:
- prompt: the self-contained instruction.
- cwd: defaults to workspace.
- extra_args: array of gemini flags. Examples:
    `["--all-files"]` — include every workspace file
    `["--model", "gemini-2.5-pro"]` — pin a model
    `["--sandbox"]` — sandboxed execution
- timeout: default 300s, capped at 600s.

ENV CONFIG:
- JANUS_GEMINI_BIN — absolute path if `gemini` isn't on PATH.
- JANUS_GEMINI_FLAGS — space-separated default flags.

PRECONDITIONS:
1. `gemini` is on PATH (`npm install -g @google/gemini-cli`).
2. An authenticated gemini session is on disk.
3. The brief is self-contained.

POSTCONDITIONS:
4. Read gemini's stdout.
5. Verify the result solved the brief before reporting done.

The capability token (`external_cli.gemini_cli: ["exec"]`) skips
the per-call y/n while this skill is active.
