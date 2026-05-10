"""
tools/claude_code.py — Claude Code CLI wrapper (v1.38.0, refactored
in v1.38.4 to use external_cli_base).

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
  * Default timeout 300s, hard-cap JANUS_EXTERNAL_CLI_TIMEOUT/600s.
  * Output capped at 50KB.

ENV OVERRIDES:
  JANUS_CLAUDE_BIN     absolute path to the claude binary

NOT IN SCOPE:
  * Streaming Claude Code output back into the parent (one-shot wrap)
  * Bidirectional handoff (would require A2A — Phase 10.4)
  * Authentication: assumes `claude login` was run; we don't manage
    credentials.
"""

from __future__ import annotations

import os

from . import base
from .. import config
from . import external_cli_base as _eb


# Re-export shared constants at module level so existing tests
# (which patch cc.MAX_OUTPUT_BYTES / cc.TIMEOUT_MAX) keep working.
DEFAULT_TIMEOUT = _eb.DEFAULT_TIMEOUT
TIMEOUT_MAX = _eb.TIMEOUT_MAX
MAX_OUTPUT_BYTES = _eb.MAX_OUTPUT_BYTES

# Re-export helpers for tests that import them directly.
shutil = _eb.shutil  # noqa: F401  — tests patch cc.shutil.which


def _strip_ansi(s: str) -> str:
    return _eb.strip_ansi(s)


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    return _eb.truncate(s, limit)


def _claude_binary() -> str | None:
    """Find claude on PATH, with JANUS_CLAUDE_BIN absolute-path override."""
    return _eb.find_binary("claude", "JANUS_CLAUDE_BIN")


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

        timeout = _eb.clamp_timeout(
            args.get("timeout"), DEFAULT_TIMEOUT, TIMEOUT_MAX,
        )

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

        ok = _eb.request_approval(
            approver=approver,
            name="claude_code",
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            extra_lines=f"format: {output_format}\n",
        )
        if not ok:
            return "claude_code: refused by user."

        cmd = [binary, "-p", prompt, "--output-format", output_format]
        return _eb.execute(
            cmd=cmd,
            cwd=cwd,
            timeout=timeout,
            name="claude_code",
            binary_path=binary,
        )
