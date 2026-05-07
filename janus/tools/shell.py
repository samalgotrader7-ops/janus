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
import os
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


def _ancestor_pids() -> set[int]:
    """v1.24.4: walk up the process tree starting at our own PID.

    Returns the set of PIDs that, if killed, would also kill Janus.
    On POSIX uses /proc/<pid>/status PPid line (no ps dependency).
    On Windows uses os.getppid() one hop. Failure-silent — if we can't
    walk the tree we return just our own PID, which still catches the
    most common case (the model's `kill <janus_pid>` self-shot).
    """
    pids: set[int] = set()
    try:
        cur = os.getpid()
        pids.add(cur)
        # Always include direct parent on POSIX + Windows.
        try:
            pids.add(os.getppid())
        except OSError:
            pass
        # On POSIX, walk up to the session leader via /proc.
        if os.name == "posix":
            for _ in range(8):  # bounded — don't loop forever on broken /proc
                try:
                    status = (
                        open(f"/proc/{cur}/status", "r", encoding="ascii")
                        .read()
                    )
                except OSError:
                    break
                ppid = None
                for line in status.splitlines():
                    if line.startswith("PPid:"):
                        try:
                            ppid = int(line.split()[1])
                        except (IndexError, ValueError):
                            ppid = None
                        break
                if ppid is None or ppid <= 1:
                    break
                pids.add(ppid)
                cur = ppid
    except Exception:
        pass
    return pids


# Process names that, if killed by name (killall / pkill -f), would
# take Janus down. Conservative — if there's any chance the pattern
# matches our own python interpreter, refuse.
_OUR_PROCESS_NAMES = {"janus", "python", "python3", "python.exe", "python3.exe"}


# kill / pkill / killall command detector.
_KILL_RE = re.compile(
    _CMD_BOUNDARY + r"(?:/[^\s|;&]*?/)?(kill|pkill|killall)(?=\s|$|[|;&])",
    re.IGNORECASE,
)


def _extract_kill_target_pids(cmd: str) -> list[tuple[int, str]]:
    """Extract numeric PIDs from kill/pkill commands in `cmd`.

    Returns a list of (pid, original_token) for every numeric arg that
    looks like a PID. Conservative — only catches `kill <num>` and
    `pkill -P <num>` shapes, not name-based killing (handled separately).
    """
    out: list[tuple[int, str]] = []
    for m in _KILL_RE.finditer(cmd):
        # Take the substring after the kill word until the next shell
        # separator (; | && >) so we don't pick up PIDs from later
        # commands in a chain.
        rest = cmd[m.end():]
        boundary = rest.find(";")
        for sep in ("|", "&", ">", "<"):
            i = rest.find(sep)
            if i >= 0 and (boundary < 0 or i < boundary):
                boundary = i
        if boundary >= 0:
            rest = rest[:boundary]
        # Tokenize on whitespace; numeric tokens are PIDs (skip flags).
        for tok in rest.split():
            if tok.startswith("-"):
                continue
            # Strip surrounding quotes.
            tok_clean = tok.strip("'\"")
            try:
                out.append((int(tok_clean), tok))
            except ValueError:
                continue
    return out


def _check_self_kill(cmd: str) -> str | None:
    """v1.24.4: refuse commands that would terminate Janus itself.

    Triggered the day Sam asked Janus to "restart janus-web" — the
    model called shell with `kill 44760 ; sleep 1 ; janus web ...` but
    44760 was Janus's own PID. The CLI immediately died ("Terminated"),
    leaving Sam looking at a bash prompt with no agent.

    Detection covers:
      * `kill <pid>` where <pid> is our PID or any ancestor.
      * `pkill -P <pid>` likewise.
      * `killall <name>` / `pkill <name>` where <name> matches our
        process name (janus, python, python3).
      * Hard-stop signals: `kill -9 ...` is checked the same way.
    """
    s = cmd.strip()
    if not s:
        return None

    matches = list(_KILL_RE.finditer(s))
    if not matches:
        return None

    danger_pids = _ancestor_pids()

    # 1. Numeric PID match.
    for pid, tok in _extract_kill_target_pids(s):
        if pid in danger_pids:
            return (
                f"refused: command would kill Janus itself "
                f"(pid {pid} is in the agent's process ancestry "
                f"{sorted(danger_pids)}). Sam's incident "
                f"2026-05-07: model ran `kill 44760` to restart janus-web "
                f"but 44760 was the Janus process itself; the CLI died "
                f"mid-task. To restart janus-web, ask the user to run "
                f"`systemctl --user restart janus-web` from a separate "
                f"shell — Janus shouldn't kill its own ancestor processes."
            )

    # 2. Name-based kill: killall / pkill <name>.
    for m in matches:
        verb = m.group(1).lower()
        if verb not in ("killall", "pkill"):
            continue
        rest = s[m.end():].lstrip()
        # Walk the args looking for an unquoted name token.
        boundary = rest.find(";")
        for sep in ("|", "&", ">", "<"):
            i = rest.find(sep)
            if i >= 0 and (boundary < 0 or i < boundary):
                boundary = i
        if boundary >= 0:
            rest = rest[:boundary]
        for tok in rest.split():
            if tok.startswith("-"):
                continue
            t = tok.strip("'\"")
            # Match against our known process names (case-insensitive).
            if t.lower() in _OUR_PROCESS_NAMES:
                return (
                    f"refused: `{verb} {t}` would kill Janus itself or "
                    f"its python interpreter. To restart a Janus "
                    f"sub-service (web / telegram / daemon), use "
                    f"`systemctl --user restart janus-<name>` or have "
                    f"the user run the restart from a separate shell."
                )
            # Substring match for safety: 'janus' inside 'janus-web' would
            # also be killed by `pkill janus`.
            if "janus" in t.lower():
                return (
                    f"refused: `{verb} {t}` would match the Janus "
                    f"process itself (substring 'janus'). Use "
                    f"`pkill -f janus-web` (more specific) only if you "
                    f"can rule out matching the agent."
                )

    return None


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

        # v1.24.4: refuse self-killing (kill <our_pid>, killall janus, etc.).
        # Same pre-approval check — the agent shouldn't be approved to
        # commit suicide.
        refusal = _check_self_kill(cmd)
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
