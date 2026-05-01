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
  4. wall-clock timeout

When we add skills in Phase 3, capability tokens will further narrow this:
a skill might declare shell.exec=['pnpm *', 'git *'] and then ONLY commands
matching those globs get approved without prompting. That's the upgrade
path; we leave the seam here.
"""

from __future__ import annotations
import subprocess
from typing import Callable

from . import base
from .. import config

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_BYTES = 50_000


class Shell(base.Tool):
    name = "shell"
    description = (
        "Execute a shell command inside the workspace directory. "
        "Captures stdout+stderr, up to 50KB. Default timeout 60s. "
        "DESTRUCTIVE — every call requires user approval. "
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
                "description": f"Optional timeout in seconds (default {DEFAULT_TIMEOUT}).",
            },
        },
        "required": ["command"],
    }
    dangerous = True
    risk = "exec"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        cmd: str = args["command"]
        timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)

        details = f"command: {cmd}\n  cwd: {config.WORKSPACE}\n  timeout: {timeout}s"
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
        return f"exit={proc.returncode}\n{out}"
