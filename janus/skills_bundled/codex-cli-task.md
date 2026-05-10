---
name: codex-cli-task
description: Hand off a focused coding sub-task to OpenAI's open-source Codex CLI via `codex exec`. Useful for terse, fast coding work.
state: quarantined
project_types:
  - any
capabilities:
  external_cli.codex_cli:
    - "exec"
created: 2026-05-10T00:00:00Z
last-promoted: null
runs: 0
success: 0
fail: 0
---

You are running codex-cli-task.

Use this skill to delegate a focused coding sub-task to OpenAI's
open-source Codex CLI (https://github.com/openai/codex) running
non-interactively via `codex exec`.

WHEN TO USE:
- Fast, terse coding tasks where you want OpenAI's GPT-grade model
  to do the work.
- Tasks where you want structured output (`extra_args=["--json"]`)
  for downstream parsing.
- Specific model overrides (`extra_args=["--model", "gpt-5"]`).

WHEN NOT TO USE:
- Non-interactive flow doesn't fit (codex exec is one-shot).
- Tasks requiring this conversation's context.

THE CODEX_CLI TOOL:
You have a `codex_cli` tool. Pass:
- prompt: the self-contained instruction.
- cwd: defaults to workspace.
- extra_args: array of codex flags inserted before the prompt.
  Examples:
    `["--json"]` — structured output envelope
    `["--model", "gpt-5"]` — pin a model
    `["--sandbox"]` — sandboxed execution (if supported)
- timeout: default 300s, capped at 600s.

ENV CONFIG:
- JANUS_CODEX_BIN — absolute path if `codex` isn't on PATH.
- JANUS_CODEX_FLAGS — space-separated default flags applied to
  every invocation. The caller's `extra_args` win on positional
  precedence (later wins for codex's flag-parsing).

PRECONDITIONS:
1. `codex` is on PATH (`npm i -g @openai/codex` or binary release).
2. An authenticated codex session is on disk.
3. The brief is self-contained.

POSTCONDITIONS:
4. Read codex's stdout. If using `--json`, parse the envelope.
5. Verify the result solved the brief before reporting done.

The capability token (`external_cli.codex_cli: ["exec"]`) skips
the per-call y/n while this skill is active.
