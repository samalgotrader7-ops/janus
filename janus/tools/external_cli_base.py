"""
tools/external_cli_base.py — shared helpers for external CLI agent
wrappers (v1.38.4, Phase 10.2.4).

WHY:
v1.38.0–v1.38.3 shipped four external CLI wrappers (claude_code,
aider, codex_cli, gemini_cli). All four duplicated ~60% of the
same logic: ANSI stripping, output truncation, binary discovery
with env-var override, subprocess invocation with timeout
handling, approval flow with capability tokens, and result
formatting. This module extracts those into reusable helpers.

The wrappers stay as separate Tool subclasses (each has its own
parameters schema and command shape), but their .run() bodies
collapse to:

    binary = find_binary("claude", "JANUS_CLAUDE_BIN")
    if not binary: return install_hint("claude_code", ...)
    cmd = [binary, "-p", prompt, "--output-format", fmt]
    return execute(
        cmd=cmd, cwd=cwd, timeout=timeout, name="claude_code",
        approver=approver, capability=("external_cli", "claude_code", "exec"),
        details=details, binary_path=binary,
    )

NO BEHAVIOR CHANGE intended in this refactor — every existing
test in v1.38.0–v1.38.3 keeps passing. The extraction is
mechanical.

ENV CONFIG (shared across all wrappers):
  JANUS_EXTERNAL_CLI_TIMEOUT  hard-cap timeout for any external CLI
                              call. Default 600s.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from typing import Iterable

from . import base


DEFAULT_TIMEOUT = 300
TIMEOUT_MAX = int(os.environ.get("JANUS_EXTERNAL_CLI_TIMEOUT", "600"))
MAX_OUTPUT_BYTES = 50_000

# Match ANSI CSI sequences. All four wrappers were carrying this.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


# ---------- output cleanup ----------


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


def truncate(s: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    if len(s) <= limit:
        return s
    return s[: limit - 64] + f"\n[... output truncated at {limit} bytes ...]"


# ---------- binary discovery ----------


def find_binary(name: str, env_var: str) -> str | None:
    """Find `name` on PATH, with optional env-var absolute-path
    override. Returns the resolved path or None.

    Priority:
      1. ``$<env_var>`` if set: try shutil.which() first (resolves
         names like 'claude.cmd' on Windows), then fall back to
         absolute-path file existence check.
      2. ``shutil.which(name)`` on PATH.
    """
    pinned = os.environ.get(env_var, "").strip()
    if pinned:
        resolved = shutil.which(pinned)
        if resolved:
            return resolved
        if os.path.isabs(pinned) and os.path.isfile(pinned):
            return pinned
    return shutil.which(name)


# ---------- arg normalization ----------


def normalize_extra_args(raw: Iterable | str | None) -> list[str]:
    """Accept extra_args as list[str], one shell-split string, or
    None. Drops empty/whitespace tokens."""
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


def env_flags(env_var: str) -> list[str]:
    """Read a space-separated flags env var into a list[str]."""
    raw = os.environ.get(env_var, "").strip()
    return shlex.split(raw) if raw else []


# ---------- timeout clamp ----------


def clamp_timeout(value, default: int = DEFAULT_TIMEOUT, cap: int = TIMEOUT_MAX) -> int:
    """Clamp timeout to [1, cap], default when value is None/garbage."""
    try:
        t = int(value) if value is not None else default
    except (ValueError, TypeError):
        t = default
    return max(1, min(t, cap))


# ---------- subprocess execution + output formatting ----------


def execute(
    *,
    cmd: list[str],
    cwd: str,
    timeout: int,
    name: str,
    binary_path: str,
) -> str:
    """Run the subprocess + format the result string the model sees.

    `name` is the tool name (e.g. 'claude_code') used in error
    messages.
    `binary_path` is included in the FileNotFoundError message so the
    user can verify with `<binary_path> --version`.

    Returns ONE formatted string covering all paths:
      * exit 0 + stdout → return stdout
      * exit 0 + empty stdout → "<name>: completed (exit 0, no stdout)"
      * exit != 0 → "<name>: exit N\\n--- stderr ---\\n…\\n--- stdout ---\\n…"
      * timeout → "<name>: timed out after Ns\\n--- partial …"
      * spawn errors → "<name>: …" with type info
    """
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
        partial_out = truncate(strip_ansi(e.stdout or ""))
        partial_err = truncate(strip_ansi(e.stderr or ""))
        return (
            f"{name}: timed out after {timeout}s.\n"
            f"--- partial stdout ---\n{partial_out}\n"
            f"--- partial stderr ---\n{partial_err}"
        )
    except FileNotFoundError:
        return (
            f"{name}: binary not executable: {binary_path}. "
            f"Verify with `{binary_path} --version` from your shell."
        )
    except OSError as e:
        return f"{name}: spawn failed: {type(e).__name__}: {e}"

    stdout = truncate(strip_ansi(proc.stdout or ""))
    stderr = truncate(strip_ansi(proc.stderr or ""))
    rc = proc.returncode

    if rc == 0:
        if not stdout.strip():
            return f"{name}: completed (exit 0, no stdout)."
        return stdout

    return (
        f"{name}: exit {rc}.\n"
        f"--- stderr ---\n{stderr}\n"
        f"--- stdout ---\n{stdout}"
    )


# ---------- approval helper ----------


def request_approval(
    *,
    approver: base.Approver,
    name: str,
    prompt: str,
    cwd: str,
    timeout: int,
    extra_lines: str = "",
) -> bool:
    """Show a consistent approval prompt across all wrappers.

    `extra_lines` is wrapper-specific extra context (e.g. flag list,
    files list, output format) — already formatted with trailing
    newline if present.
    """
    details = (
        f"prompt: {prompt[:200]}{'…' if len(prompt) > 200 else ''}\n"
        f"cwd:    {cwd}\n"
        f"{extra_lines}"
        f"timeout: {timeout}s"
    )
    return approver(
        f"run {name}",
        details,
        capability=("external_cli", name, "exec"),
    )
