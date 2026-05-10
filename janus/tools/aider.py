"""
tools/aider.py — Aider CLI wrapper (v1.38.1, Phase 10.2.1).

WHY:
Aider is a popular open-source CLI coding agent that pairs an LLM
with git for explicit, traceable file edits. Wrapping it lets
Janus delegate focused refactors / multi-file edits to aider's
proven workflow. Like the v1.38.0 Claude Code wrapper, the
integration uses aider's non-interactive mode:
  ``aider --message <prompt> --yes-always [--file <f> ...]``

USAGE FROM THE MODEL'S PERSPECTIVE:
The model passes a prompt + optional file list. Aider runs in the
cwd, processes the message non-interactively, captures stdout.
The ``files`` arg is the standard aider knob — without it, aider
explores the repo automatically; with it, aider focuses on the
listed files.

SAFETY:
  * dangerous=True — aider edits files + auto-commits to git.
  * risk='exec' — fits the standard mode matrix.
  * Capability ("external_cli", "aider", "exec").
  * cwd must exist; defaults to config.WORKSPACE.
  * Timeout default 300s, hard-cap JANUS_EXTERNAL_CLI_TIMEOUT.
  * Output capped at 50KB.

NOT IN SCOPE FOR v1.38.1:
  * --architect / --editor split (let users opt in via a custom
    skill that pins extra args).
  * Granular control over aider's git behavior (auto-commits, etc.)
  * Custom model selection per call (use AIDER_MODEL env var instead).
"""

from __future__ import annotations

import os
import re
import shutil
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


def _aider_binary() -> str | None:
    """Find aider on PATH, with JANUS_AIDER_BIN override."""
    pinned = os.environ.get("JANUS_AIDER_BIN", "").strip()
    if pinned:
        if shutil.which(pinned):
            return shutil.which(pinned)
        if os.path.isabs(pinned) and os.path.isfile(pinned):
            return pinned
    return shutil.which("aider")


def _normalize_files(raw: Iterable | str | None) -> list[str]:
    """Accept files as list[str], str (one path), or None."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    out: list[str] = []
    try:
        for item in raw:
            s = str(item).strip()
            if s:
                out.append(s)
    except TypeError:
        pass
    return out


class Aider(base.Tool):
    name = "aider"
    description = (
        "Hand off a focused coding sub-task to Aider "
        "(https://aider.chat) running non-interactively via "
        "`aider --message`. Useful for git-tracked refactors and "
        "multi-file edits where you want explicit commits. "
        "Optionally pin specific files with the `files` arg so aider "
        "scopes its context. Returns aider's stdout (ANSI-stripped). "
        "Requires `aider` on PATH (`pip install aider-chat`). "
        "DESTRUCTIVE — aider edits + auto-commits files. Every call "
        "requires user approval. Default timeout 300s, hard-capped at "
        "JANUS_EXTERNAL_CLI_TIMEOUT (default 600s)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": (
                    "The instruction to send aider. Plain text; aider "
                    "decides which files to touch (unless `files` is "
                    "given to scope it)."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory aider runs in. Must be inside a "
                    "git repo for aider's auto-commit flow to work. "
                    "Defaults to the current Janus workspace."
                ),
            },
            "files": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of file paths (relative to cwd) for "
                    "aider to focus on. Without this, aider explores "
                    "the repo automatically. Pin files to keep context "
                    "tight on big repos."
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
            return "aider: empty prompt"

        cwd = (args.get("cwd") or "").strip() or str(config.WORKSPACE)
        if not os.path.isdir(cwd):
            return f"aider: cwd does not exist: {cwd}"

        try:
            timeout = int(args.get("timeout") or DEFAULT_TIMEOUT)
        except (ValueError, TypeError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, min(timeout, TIMEOUT_MAX))

        files = _normalize_files(args.get("files"))

        binary = _aider_binary()
        if not binary:
            return (
                "aider: `aider` binary not found on PATH. Install with "
                "`pip install aider-chat` (or `pipx install aider-chat`) "
                "or set JANUS_AIDER_BIN to its absolute path."
            )

        # Build the command. --yes-always disables aider's per-edit
        # confirmation prompts (the user already approved at the
        # Janus level). --no-stream keeps stdout simple to capture.
        cmd: list[str] = [
            binary,
            "--message", prompt,
            "--yes-always",
            "--no-stream",
        ]
        for f in files:
            cmd.extend(["--file", f])

        # Approval prompt — show prompt + cwd + files + timeout.
        files_summary = (
            f"   files: {', '.join(files[:5])}{'…' if len(files) > 5 else ''}"
            if files else ""
        )
        details = (
            f"prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}\n"
            f"cwd:    {cwd}{files_summary}\n"
            f"timeout: {timeout}s"
        )
        ok = approver(
            "run aider",
            details,
            capability=("external_cli", "aider", "exec"),
        )
        if not ok:
            return "aider: refused by user."

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
                f"aider: timed out after {timeout}s.\n"
                f"--- partial stdout ---\n{partial_out}\n"
                f"--- partial stderr ---\n{partial_err}"
            )
        except FileNotFoundError:
            return (
                f"aider: binary not executable: {binary}. "
                f"Verify with `{binary} --version` from your shell."
            )
        except OSError as e:
            return f"aider: spawn failed: {type(e).__name__}: {e}"

        stdout = _truncate(_strip_ansi(proc.stdout or ""))
        stderr = _truncate(_strip_ansi(proc.stderr or ""))
        rc = proc.returncode

        if rc == 0:
            if not stdout.strip():
                return "aider: completed (exit 0, no stdout)."
            return stdout

        return (
            f"aider: exit {rc}.\n"
            f"--- stderr ---\n{stderr}\n"
            f"--- stdout ---\n{stdout}"
        )
