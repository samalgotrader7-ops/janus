"""
tools/gemini_cli.py — Google Gemini CLI wrapper (v1.38.3, Phase 10.2.3).

WHY:
Wraps Google's open-source gemini-cli
(https://github.com/google-gemini/gemini-cli) so Janus can hand
focused tasks to Gemini's coding agent. Non-interactive mode is
``gemini -p "<prompt>"`` — symmetric with claude_code's ``-p``
flag (and the historical "print mode" pattern across these CLIs).

USAGE:
The model passes a prompt + optional cwd / timeout / extra_args.
We run ``gemini -p [extra_args...] <prompt>`` in the cwd.

EXTRA ARGS:
gemini-cli supports ``--all-files`` (include every workspace file
in context), ``--model <id>`` (pin a specific Gemini model),
``--sandbox`` (sandboxed execution), etc. Janus exposes these via
the ``extra_args`` array param so the model can opt in.

ENV OVERRIDES:
  JANUS_GEMINI_BIN     absolute path to the gemini binary
  JANUS_GEMINI_FLAGS   space-separated default flags

SAFETY:
  * dangerous=True — gemini can edit files in cwd
  * risk='exec'
  * Capability ("external_cli", "gemini_cli", "exec")
  * Default timeout 300s; cap JANUS_EXTERNAL_CLI_TIMEOUT/600s
  * 50KB output truncation, ANSI strip

NOT IN SCOPE FOR v1.38.3:
  * gemini chat sessions (multi-turn) — one-shot wrap only
  * gemini's tool-call output — captured as raw stdout
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


def _gemini_binary() -> str | None:
    pinned = os.environ.get("JANUS_GEMINI_BIN", "").strip()
    if pinned:
        if shutil.which(pinned):
            return shutil.which(pinned)
        if os.path.isabs(pinned) and os.path.isfile(pinned):
            return pinned
    return shutil.which("gemini")


def _normalize_extra_args(raw: Iterable | str | None) -> list[str]:
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
    raw = os.environ.get("JANUS_GEMINI_FLAGS", "").strip()
    return shlex.split(raw) if raw else []


class GeminiCli(base.Tool):
    name = "gemini_cli"
    description = (
        "Hand off a focused coding sub-task to Google's Gemini CLI "
        "(https://github.com/google-gemini/gemini-cli) running "
        "non-interactively via `gemini -p`. Returns gemini's stdout "
        "(ANSI-stripped). Requires `gemini` on PATH (`npm install -g "
        "@google/gemini-cli`) and an authenticated session. "
        "DESTRUCTIVE — gemini can edit files in cwd. Every call "
        "requires user approval. Default timeout 300s, hard-capped at "
        "JANUS_EXTERNAL_CLI_TIMEOUT (default 600s). "
        "Use `extra_args` to pass gemini flags like '--all-files' "
        "(include all workspace files in context), '--model' "
        "(pin a specific Gemini model), or '--sandbox'."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send Gemini. Plain text; "
                    "Gemini decides which files to read/write."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory gemini runs in. Defaults to "
                    "the current Janus workspace."
                ),
            },
            "extra_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Additional gemini flags inserted between `-p` "
                    "and the prompt. Examples: ['--all-files'] for "
                    "full context, ['--model', 'gemini-2.5-pro'] to "
                    "pin a model. Caller wins over JANUS_GEMINI_FLAGS."
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
            return "gemini_cli: empty prompt"

        cwd = (args.get("cwd") or "").strip() or str(config.WORKSPACE)
        if not os.path.isdir(cwd):
            return f"gemini_cli: cwd does not exist: {cwd}"

        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (ValueError, TypeError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, TIMEOUT_MAX))

        extra = _normalize_extra_args(args.get("extra_args"))
        env_flags = _env_flags()

        binary = _gemini_binary()
        if not binary:
            return (
                "gemini_cli: `gemini` binary not found on PATH. "
                "Install with `npm install -g @google/gemini-cli` or "
                "set JANUS_GEMINI_BIN to its absolute path."
            )

        # Command shape: gemini -p [env_flags...] [extra_args...] <prompt>
        cmd = [binary, "-p"] + env_flags + extra + [prompt]

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
            "run gemini_cli",
            details,
            capability=("external_cli", "gemini_cli", "exec"),
        )
        if not ok:
            return "gemini_cli: refused by user."

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
                f"gemini_cli: timed out after {timeout}s.\n"
                f"--- partial stdout ---\n{partial_out}\n"
                f"--- partial stderr ---\n{partial_err}"
            )
        except FileNotFoundError:
            return (
                f"gemini_cli: binary not executable: {binary}. "
                f"Verify with `{binary} --version` from your shell."
            )
        except OSError as e:
            return f"gemini_cli: spawn failed: {type(e).__name__}: {e}"

        stdout = _truncate(_strip_ansi(proc.stdout or ""))
        stderr = _truncate(_strip_ansi(proc.stderr or ""))
        rc = proc.returncode

        if rc == 0:
            if not stdout.strip():
                return "gemini_cli: completed (exit 0, no stdout)."
            return stdout

        return (
            f"gemini_cli: exit {rc}.\n"
            f"--- stderr ---\n{stderr}\n"
            f"--- stdout ---\n{stdout}"
        )
