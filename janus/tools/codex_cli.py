"""
tools/codex_cli.py — OpenAI Codex CLI wrapper (v1.38.2, Phase 10.2.2).

WHY:
Sam confirmed (2026-05-10): we wrap openai/codex — the open-source
agent CLI from OpenAI (GitHub: openai/codex). Non-interactive
mode is ``codex exec "<prompt>"`` — same shape as claude_code's
``-p`` and aider's ``--message``.

USAGE:
The model passes a prompt + optional cwd / timeout / extra args.
We run ``codex exec [extra_args...] <prompt>`` in the cwd.

SAFETY:
Same posture as claude_code / aider:
  * dangerous=True — codex can edit files in cwd
  * risk='exec'
  * Capability ("external_cli", "codex_cli", "exec")
  * Default timeout 300s; cap JANUS_EXTERNAL_CLI_TIMEOUT/600s
  * 50KB output truncation, ANSI strip

EXTRA ARGS:
codex's non-interactive mode supports flags like --json (structured
output), --model <id> (override the underlying model). Janus
exposes these via an `extra_args` array param so the model can
opt in without us hard-coding the full flag matrix.

ENV OVERRIDES:
  JANUS_CODEX_BIN     absolute path to the codex binary
  JANUS_CODEX_FLAGS   space-separated flags appended to every call
                      (e.g. '--model gpt-5'). Caller `extra_args`
                      arg appends AFTER these so caller wins.

NOT IN SCOPE FOR v1.38.2:
  * Streaming codex output back into Janus's chat (one-shot wrap)
  * Codex's project-level config (.codexrc) parsing — we let the
    binary discover its own config from cwd as it does normally
"""

from __future__ import annotations

import os
import re
import shutil
import shlex
import subprocess
from typing import Iterable

from . import base
from .. import config


DEFAULT_TIMEOUT = 300
TIMEOUT_MAX = int(os.environ.get("JANUS_EXTERNAL_CLI_TIMEOUT", "600"))
MAX_OUTPUT_BYTES = 50_000

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 64] + f"\n[... output truncated at {limit} bytes ...]"


def _codex_binary() -> str | None:
    """Find codex on PATH, with JANUS_CODEX_BIN override."""
    pinned = os.environ.get("JANUS_CODEX_BIN", "").strip()
    if pinned:
        if shutil.which(pinned):
            return shutil.which(pinned)
        if os.path.isabs(pinned) and os.path.isfile(pinned):
            return pinned
    return shutil.which("codex")


def _normalize_extra_args(raw: Iterable | str | None) -> list[str]:
    """Accept extra_args as list[str], one shell-split string, or None."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return shlex.split(raw)
    out: list[str] = []
    try:
        for item in raw:
            s = str(item).strip()
            if s:
                out.append(s)
    except TypeError:
        pass
    return out


def _env_flags() -> list[str]:
    raw = os.environ.get("JANUS_CODEX_FLAGS", "").strip()
    return shlex.split(raw) if raw else []


class CodexCli(base.Tool):
    name = "codex_cli"
    description = (
        "Hand off a focused coding sub-task to OpenAI's Codex CLI "
        "(https://github.com/openai/codex) running non-interactively "
        "via `codex exec`. Returns codex's stdout (ANSI-stripped). "
        "Requires `codex` on PATH (`npm i -g @openai/codex` or the "
        "binary release) and an authenticated session. DESTRUCTIVE — "
        "codex can edit files in cwd. Every call requires user "
        "approval. Default timeout 300s, hard-capped at "
        "JANUS_EXTERNAL_CLI_TIMEOUT (default 600s). "
        "Use `extra_args` to pass codex flags like '--json' for "
        "structured output or '--model <id>' to override its model."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send codex. Plain text; codex "
                    "decides which files to read/write."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory codex runs in. Defaults to the "
                    "current Janus workspace."
                ),
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional codex flags inserted between `exec` "
                    "and the prompt. Examples: ['--json'] for "
                    "structured output, ['--model', 'gpt-5'] to pin "
                    "a specific model. Caller wins over "
                    "JANUS_CODEX_FLAGS."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Wall-clock timeout in seconds. Default "
                    f"{DEFAULT_TIMEOUT}, hard-capped at "
                    f"JANUS_EXTERNAL_CLI_TIMEOUT (default {TIMEOUT_MAX}s)."
                ),
            },
        },
        "required": ["prompt"],
    }
    dangerous = True
    risk = "exec"

    def run(self, args: dict, approver: base.Approver) -> str:
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return "codex_cli: empty prompt"

        cwd = (args.get("cwd") or "").strip() or str(config.WORKSPACE)
        if not os.path.isdir(cwd):
            return f"codex_cli: cwd does not exist: {cwd}"

        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (ValueError, TypeError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, TIMEOUT_MAX))

        extra = _normalize_extra_args(args.get("extra_args"))
        env_flags = _env_flags()

        binary = _codex_binary()
        if not binary:
            return (
                "codex_cli: `codex` binary not found on PATH. Install "
                "from https://github.com/openai/codex (npm or binary "
                "release) or set JANUS_CODEX_BIN to its absolute path."
            )

        # Command shape: codex exec [env_flags...] [extra_args...] <prompt>
        # extra_args appended AFTER env_flags so caller overrides.
        cmd = [binary, "exec"] + env_flags + extra + [prompt]

        # Approval prompt with all the inputs visible.
        flags_summary = ""
        if env_flags or extra:
            shown = (env_flags + extra)[:6]
            ellipsis = "…" if len(env_flags + extra) > 6 else ""
            flags_summary = f"   flags: {' '.join(shown)}{ellipsis}\n"
        details = (
            f"prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}\n"
            f"cwd:    {cwd}\n"
            f"{flags_summary}"
            f"timeout: {timeout}s"
        )
        ok = approver(
            "run codex_cli",
            details,
            capability=("external_cli", "codex_cli", "exec"),
        )
        if not ok:
            return "codex_cli: refused by user."

        try:
            proc = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            partial_out = _truncate(_strip_ansi(e.stdout or ""))
            partial_err = _truncate(_strip_ansi(e.stderr or ""))
            return (
                f"codex_cli: timed out after {timeout}s.\n"
                f"--- partial stdout ---\n{partial_out}\n"
                f"--- partial stderr ---\n{partial_err}"
            )
        except FileNotFoundError:
            return (
                f"codex_cli: binary not executable: {binary}. "
                f"Verify with `{binary} --version` from your shell."
            )
        except OSError as e:
            return f"codex_cli: spawn failed: {type(e).__name__}: {e}"

        stdout = _truncate(_strip_ansi(proc.stdout or ""))
        stderr = _truncate(_strip_ansi(proc.stderr or ""))
        rc = proc.returncode

        if rc == 0:
            if not stdout.strip():
                return "codex_cli: completed (exit 0, no stdout)."
            return stdout

        return (
            f"codex_cli: exit {rc}.\n"
            f"--- stderr ---\n{stderr}\n"
            f"--- stdout ---\n{stdout}"
        )
