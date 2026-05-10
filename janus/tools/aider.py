"""
tools/aider.py — Aider CLI wrapper (v1.38.1, refactored in v1.38.4
to use external_cli_base).

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

ENV OVERRIDES:
  JANUS_AIDER_BIN     absolute path to the aider binary
"""

from __future__ import annotations

import os
from typing import Iterable

from . import base
from .. import config
from . import external_cli_base as _eb


DEFAULT_TIMEOUT = _eb.DEFAULT_TIMEOUT
TIMEOUT_MAX = _eb.TIMEOUT_MAX
MAX_OUTPUT_BYTES = _eb.MAX_OUTPUT_BYTES

shutil = _eb.shutil  # noqa: F401  — tests patch ai.shutil.which


def _strip_ansi(s: str) -> str:
    return _eb.strip_ansi(s)


def _truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    return _eb.truncate(s, limit)


def _aider_binary() -> str | None:
    """Find aider on PATH, with JANUS_AIDER_BIN override."""
    return _eb.find_binary("aider", "JANUS_AIDER_BIN")


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

        timeout = _eb.clamp_timeout(
            args.get("timeout"), DEFAULT_TIMEOUT, TIMEOUT_MAX,
        )
        files = _normalize_files(args.get("files"))

        binary = _aider_binary()
        if not binary:
            return (
                "aider: `aider` binary not found on PATH. Install with "
                "`pip install aider-chat` (or `pipx install aider-chat`) "
                "or set JANUS_AIDER_BIN to its absolute path."
            )

        files_summary = (
            f"   files: {', '.join(files[:5])}{'…' if len(files) > 5 else ''}\n"
            if files else ""
        )
        ok = _eb.request_approval(
            approver=approver,
            name="aider",
            prompt=prompt,
            cwd=cwd,
            timeout=timeout,
            extra_lines=files_summary,
        )
        if not ok:
            return "aider: refused by user."

        cmd: list[str] = [
            binary,
            "--message", prompt,
            "--yes-always",
            "--no-stream",
        ]
        for f in files:
            cmd.extend(["--file", f])

        return _eb.execute(
            cmd=cmd,
            cwd=cwd,
            timeout=timeout,
            name="aider",
            binary_path=binary,
        )
