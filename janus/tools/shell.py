"""
tools/shell.py — shell command execution.

SECURITY POSTURE:
Shell is the highest-risk tool. We mark the entire tool dangerous=True
which means EVERY call goes through the approver — no danger heuristic
to bypass, no clever blacklist regex.

Hermes uses ~80 regex patterns to detect dangerous commands. Researchers
have repeatedly shown such lists are bypassable (`r''m -rf /`, base64-piped,
unicode lookalikes, etc.). We don't play that game. Defense is structural:
  1. cwd is locked to WORKSPACE
  2. every command needs explicit y/n
  3. output is captured, not streamed (no interactive escape)
  4. wall-clock timeout, hard-capped (config.SHELL_TIMEOUT_MAX)
  5. recursive `janus` invocations refused (would deadlock the parent
     and orphan daemon subprocesses — see v1.1.1 bug)

When we add skills in Phase 3, capability tokens will further narrow this:
a skill might declare shell.exec=['pnpm *', 'git *'] and then ONLY commands
matching those globs get approved without prompting. That's the upgrade
path; we leave the seam here.
"""

from __future__ import annotations
import re
import subprocess
from typing import Callable

from . import base
from .. import config

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_BYTES = 50_000


# Subcommands that are safe to invoke as `janus <sub>` from inside janus.
# Everything else (chat, telegram, web, whatsapp, daemon, headless `-p`,
# eval) either recurses into another agent loop or starts a long-lived
# daemon — both block subprocess.run forever.
_SAFE_JANUS_SUBCOMMANDS = frozenset({
    "--version", "-V",
    "--help", "-h", "help",
    "--logo",
    "--analyze", "-a",
    "--conversations",
    "--reindex",
    "--doctor",  # not currently a sub but might become one
})


# `janus` only counts as recursion when it's in COMMAND position — at
# start of the line or after a shell statement separator (; && || | &).
# Plain whitespace doesn't qualify, so `echo janus`, `cat janus.py`, and
# `grep janus README.md` all correctly pass through (janus is an arg,
# not the command being run).
_CMD_BOUNDARY = r"(?:^|[;|&])\s*"

_PYTHON_M_JANUS_RE = re.compile(
    _CMD_BOUNDARY
    + r"(?:[^\s|;&]*python[\d.]*)"   # python / python3 / python3.12 / .../python
    + r"\s+-m\s+janus(?=\s|$|[|;&])"  # -m janus, then arg-boundary
)
_BARE_JANUS_RE = re.compile(
    _CMD_BOUNDARY
    + r"(?:[^\s|;&]*?)"              # optional path prefix
    + r"janus(?:\.exe|\.cmd|\.py)?"  # the binary name
    + r"(?=\s|$|[|;&])"              # arg-boundary
)


def _check_recursive_janus(cmd: str) -> str | None:
    """Return None if safe; a refusal reason if the command would invoke
    janus recursively in an unsafe way.

    The model has historically tried to "test the agent" by running
    `janus telegram` from inside `janus` — that subprocess.run blocks
    until the daemon exits (never), so the parent hangs, and the orphan
    keeps polling Telegram after Ctrl+C. Refusing here prevents both.
    """
    s = cmd.strip()
    if not s:
        return None

    # Find all janus invocations in the command (covers a && b chains).
    matches = list(_PYTHON_M_JANUS_RE.finditer(s)) + list(_BARE_JANUS_RE.finditer(s))
    if not matches:
        return None

    for m in matches:
        # Look at the next token after the janus invocation.
        rest = s[m.end():].lstrip()
        # Strip out shell separators / redirects for the lookahead.
        next_tok = rest.split(None, 1)[0] if rest else ""
        # Trim trailing punctuation that shells use as separators.
        next_tok = next_tok.rstrip(";|&")
        if next_tok in _SAFE_JANUS_SUBCOMMANDS:
            continue
        return (
            f"refused: command would invoke janus recursively "
            f"(`{m.group().strip()}{(' ' + next_tok) if next_tok else ''}`). "
            f"Running `janus`, `janus telegram`, `janus web`, `janus -p ...` "
            f"etc. from inside janus blocks the parent until the child "
            f"exits — daemons never exit, and Ctrl+C orphans them. "
            f"If you want the user to run a janus subcommand, tell them "
            f"to run it in a separate terminal. "
            f"Safe read-only subcommands: {', '.join(sorted(_SAFE_JANUS_SUBCOMMANDS))}."
        )
    return None


class Shell(base.Tool):
    name = "shell"
    description = (
        "Execute a shell command inside the workspace directory. "
        "Captures stdout+stderr, up to 50KB. Default timeout 60s, "
        "hard-capped at JANUS_SHELL_TIMEOUT_MAX (default 300s). "
        "DESTRUCTIVE — every call requires user approval. "
        "Refuses recursive `janus` invocations (would deadlock the parent). "
        "Prefer specific commands over chains; the user reviews each one."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The full command line to execute, as you'd type in a shell.",
            },
            "timeout": {
                "type": "integer",
                "description": (
                    f"Optional wall-clock timeout in SECONDS (NOT ms). "
                    f"Default {DEFAULT_TIMEOUT}s. Hard cap "
                    f"{config.SHELL_TIMEOUT_MAX}s — larger values are clamped. "
                    f"Long-running daemons (telegram bots, web servers) "
                    f"should not be launched through this tool — they "
                    f"block until they exit."
                ),
            },
        },
        "required": ["command"],
    }
    dangerous = True
    risk = "exec"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        cmd: str = args["command"]

        # Refuse recursive janus invocations BEFORE asking for approval.
        # The user shouldn't even see the y/N prompt for something that
        # would deadlock anyway.
        refusal = _check_recursive_janus(cmd)
        if refusal:
            return refusal

        # Clamp timeout. The default applies when the model omits or
        # passes 0. If the model passes a large value (the v1.1 incident:
        # 600000 → 166 hours), clamp to SHELL_TIMEOUT_MAX.
        requested = int(args.get("timeout") or DEFAULT_TIMEOUT)
        timeout = min(max(1, requested), config.SHELL_TIMEOUT_MAX)
        clamped = requested != timeout

        details = f"command: {cmd}\n  cwd: {config.WORKSPACE}\n  timeout: {timeout}s"
        if clamped:
            details += (
                f"  (clamped from requested {requested}s — "
                f"max is {config.SHELL_TIMEOUT_MAX}s)"
            )
        if not approver(
            "shell exec",
            details,
            capability=("shell", "exec", cmd),
        ):
            return f"refused by user: {cmd}"

        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                cwd=config.WORKSPACE,
                capture_output=True,
                timeout=timeout,
                text=True,
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            return f"error: command timed out after {timeout}s"

        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        if len(out) > MAX_OUTPUT_BYTES:
            out = out[:MAX_OUTPUT_BYTES] + f"\n[truncated; total was {len(out)} bytes]"
        prefix = f"exit={proc.returncode}"
        if clamped:
            prefix = (
                f"[note: timeout clamped from {requested}s to {timeout}s "
                f"(JANUS_SHELL_TIMEOUT_MAX)]\n" + prefix
            )
        return f"{prefix}\n{out}"
