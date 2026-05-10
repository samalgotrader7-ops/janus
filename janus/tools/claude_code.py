"""
tools/claude_code.py — Claude Code CLI wrapper (v1.38.0, Phase 10.2.0).

WHY:
Sam wants Janus to ORCHESTRATE other coding agents, not just be one
in isolation. Phase 10.2 wraps external coding-agent CLIs as
first-class tools so Janus can hand off focused sub-tasks. Claude
Code is the marquee integration — invoked via its non-interactive
``-p`` Print Mode.

USAGE FROM THE MODEL'S PERSPECTIVE:
The model sees a "claude_code" tool. It passes a prompt, optionally
a cwd, optional timeout, and an output_format. The tool executes
``claude -p <prompt> --output-format <fmt>`` in the given cwd,
captures stdout (ANSI-stripped), returns the text. JSON output
mode is round-tripped so a structured response stays parseable.

SAFETY:
  * dangerous=True — every call needs approval, since Claude Code
    can edit files inside the cwd.
  * risk='exec' — fits the standard mode matrix.
  * Capability ("external_cli", "claude_code", "exec") — skills
    can pre-grant.
  * cwd defaults to config.WORKSPACE; if a caller passes a path
    OUTSIDE the workspace, we accept it (delegating to Claude
    Code's own boundary) — the user sees the path in the approval
    prompt and decides.
  * Timeout enforced at the subprocess level; default 300s, hard-
    cap at JANUS_EXTERNAL_CLI_TIMEOUT (env, default 600s).
  * Output capped at MAX_OUTPUT_BYTES (50KB) — same limit shell
    uses, so big logs from Claude Code don't overrun the context.

NOT IN SCOPE FOR v1.38.0:
  * Streaming Claude Code output back into the parent (the wrap
    is one-shot — caller waits for completion).
  * Bidirectional handoff (Claude Code asking Janus questions
    mid-task). That'd require A2A (Phase 10.4).
  * Authentication: assumes the user has run ``claude login`` and
    a session is on disk. We don't manage credentials.
"""

from __future__ import annotations

import os
import re
import subprocess
import shutil

from . import base
from .. import config


DEFAULT_TIMEOUT = 300
TIMEOUT_MAX = int(os.environ.get("JANUS_EXTERNAL_CLI_TIMEOUT", "600"))
MAX_OUTPUT_BYTES = 50_000

# ANSI escape sequences (CSI). Claude Code colors its terminal output;
# strip before returning to the model so the text stays clean.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 64] + f"\n[... output truncated at {limit} bytes ...]"


def _claude_binary() -> str | None:
    """Find the `claude` binary on PATH, or None if missing.

    JANUS_CLAUDE_BIN env var lets the user pin a specific path
    (useful when the binary isn't on the system PATH but the
    user knows where it lives — e.g. ~/.local/bin/claude).
    """
    pinned = os.environ.get("JANUS_CLAUDE_BIN", "").strip()
    if pinned:
        if shutil.which(pinned):
            return shutil.which(pinned)
        # Allow absolute path even if shutil.which doesn't resolve it
        if os.path.isabs(pinned) and os.path.isfile(pinned):
            return pinned
    return shutil.which("claude")


class ClaudeCode(base.Tool):
    name = "claude_code"
    description = (
        "Hand off a focused coding sub-task to Anthropic's Claude Code "
        "CLI (https://claude.com/claude-code) running in non-interactive "
        "Print Mode (`claude -p`). Useful when Janus orchestrates and "
        "wants Claude Code to write/edit code in a specific directory. "
        "Returns Claude Code's stdout (ANSI-stripped). "
        "Requires the user to have `claude` on PATH and an active session "
        "(run `claude login` once). DESTRUCTIVE — can edit files inside "
        "the cwd. Every call requires user approval. "
        "Default timeout 300s, hard-capped at JANUS_EXTERNAL_CLI_TIMEOUT "
        "(default 600s)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send Claude Code. Plain text; "
                    "Claude Code will choose tools to use. Treat this "
                    "like the message you'd type into Claude Code "
                    "interactively."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory Claude Code runs in. Defaults to "
                    "the current Janus workspace. Use a project root "
                    "or a specific subdir to scope where Claude Code "
                    "can read/write."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Wall-clock timeout in seconds. Default "
                    f"{DEFAULT_TIMEOUT}. Hard-capped at JANUS_EXTERNAL_CLI_"
                    f"TIMEOUT (default {TIMEOUT_MAX}s)."
                ),
            },
            "output_format": {
                "type": "string",
                "enum": ["text", "json"],
                "description": (
                    "Claude Code's --output-format. 'text' (default) "
                    "returns the assistant's reply as plain text. "
                    "'json' returns Claude Code's structured envelope "
                    "with metadata. Use 'json' when you need to parse "
                    "tool calls or session id; 'text' for human-readable "
                    "code edits / explanations."
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
            return "claude_code: empty prompt"

        cwd = (args.get("cwd") or "").strip() or str(config.WORKSPACE)
        if not os.path.isdir(cwd):
            return f"claude_code: cwd does not exist: {cwd}"

        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (ValueError, TypeError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, TIMEOUT_MAX))

        output_format = str(args.get("output_format") or "text").lower()
        if output_format not in ("text", "json"):
            output_format = "text"

        binary = _claude_binary()
        if not binary:
            return (
                "claude_code: `claude` binary not found on PATH. "
                "Install Claude Code from https://claude.com/claude-code "
                "or set JANUS_CLAUDE_BIN to its absolute path. After "
                "install, run `claude login` once to authenticate."
            )

        # Approval prompt — the user sees the prompt + cwd + timeout
        # before Claude Code runs. Capability tokens can pre-approve
        # via skills.
        details = (
            f"prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}\n"
            f"cwd:    {cwd}\n"
            f"timeout: {timeout}s   format: {output_format}"
        )
        ok = approver(
            "run claude_code",
            details,
            capability=("external_cli", "claude_code", "exec"),
        )
        if not ok:
            return "claude_code: refused by user."

        cmd = [binary, "-p", prompt, "--output-format", output_format]
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
                f"claude_code: timed out after {timeout}s.\n"
                f"--- partial stdout ---\n{partial_out}\n"
                f"--- partial stderr ---\n{partial_err}"
            )
        except FileNotFoundError:
            return (
                f"claude_code: binary not executable: {binary}. "
                f"Verify with `{binary} --version` from your shell."
            )
        except OSError as e:
            return f"claude_code: spawn failed: {type(e).__name__}: {e}"

        stdout = _truncate(_strip_ansi(proc.stdout or ""))
        stderr = _truncate(_strip_ansi(proc.stderr or ""))
        rc = proc.returncode

        if rc == 0:
            # Happy path — return stdout. If Claude Code wrote nothing
            # (rare), include a short status so the parent agent
            # doesn't think the call silently no-op'd.
            if not stdout.strip():
                return "claude_code: completed (exit 0, no stdout)."
            return stdout

        # Non-zero exit — show stderr first so the failure reason
        # leads. Include stdout below for context.
        return (
            f"claude_code: exit {rc}.\n"
            f"--- stderr ---\n{stderr}\n"
            f"--- stdout ---\n{stdout}"
        )
