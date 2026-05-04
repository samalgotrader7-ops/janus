"""
tools/shell_bg.py — background shell + output monitor + kill (v1.15.0).

WHY THIS EXISTS:
Pre-v1.15 the shell tool was synchronous: model launches `npm run build`,
the executor blocks for the build's lifetime (often minutes), nothing
else can happen, and if the build hangs the whole turn hangs.

Claude Code solves this with `Bash run_in_background=true` + `BashOutput
shell_id=...` + `KillShell shell_id=...`. Three tools forming a state
machine: spawn → poll → terminate. v1.15 ports the pattern.

USE CASES:
- builds / test suites / type-checks where the model wants to read
  output progressively
- dev servers / watch tasks where the model wants to start something
  and check it later
- long-running data pipelines where the model wants to do other work
  in parallel

DESIGN — ONE PROCESS REGISTRY, FILE-BACKED OUTPUT:
We don't pile a Python-side queue per shell — too many failure modes
(model never polls, process never dies). Instead:

  ~/.janus/shells/<shell_id>/
    pid             — process pid
    cmd             — command launched
    started         — ISO timestamp
    cwd             — working directory at spawn
    stdout.log      — captured stdout (line-buffered)
    stderr.log      — captured stderr
    status          — 'running' | 'exited:<code>' | 'killed' | 'timeout'

shell_run_bg launches subprocess.Popen with stdout/stderr → log files,
records pid + metadata, returns shell_id.

shell_output reads NEW bytes from stdout/stderr since the last poll
(persistent offset stored alongside).

shell_kill SIGTERMs the pid (SIGKILL after 5s grace).

P5 (plain-text state): shell_id directory is cat-able. User can
`cat ~/.janus/shells/<id>/stdout.log` while the model is doing other
work.

P7 (bounded everything):
- max concurrent shells: SHELL_BG_MAX_RUNNING (default 5)
- max output retained per shell: SHELL_BG_MAX_OUTPUT_BYTES (default 1 MB)
  (older bytes truncated when re-read, but disk file keeps growing —
  bounded by the OS, not us)
- max total background shells in registry (oldest pruned): 50
- timeout enforced via WALLCLOCK kill at SHELL_BG_MAX_WALLCLOCK_S (1h)
- reject if more than max running already

Cross-platform notes:
- POSIX: subprocess.Popen with start_new_session=True so SIGTERM-ing
  the pid kills the WHOLE process group (not just bash). Otherwise
  `node server.js &` style backgrounding inside the bash leaves
  the actual node process orphaned.
- Windows: Popen with creationflags=DETACHED_PROCESS for similar
  detachment. SIGTERM doesn't exist; we use proc.terminate() which
  maps to TerminateProcess.
"""

from __future__ import annotations
import datetime as dt
import os
import secrets
import shlex
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import base
from .. import config


# Limits.
SHELL_BG_MAX_RUNNING = int(os.getenv("JANUS_SHELL_BG_MAX_RUNNING", "5"))
SHELL_BG_MAX_OUTPUT_BYTES = int(
    os.getenv("JANUS_SHELL_BG_MAX_OUTPUT_BYTES", str(1024 * 1024))
)
SHELL_BG_MAX_WALLCLOCK_S = int(
    os.getenv("JANUS_SHELL_BG_MAX_WALLCLOCK", "3600")
)
SHELL_BG_REGISTRY_MAX = 50


def _shells_root() -> Path:
    return config.HOME / "shells"


def _shell_dir(shell_id: str) -> Path:
    return _shells_root() / shell_id


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _new_shell_id() -> str:
    """Sortable + readable id: <ts>__<hex>."""
    return _now_iso().replace(":", "-") + "__" + secrets.token_hex(3)


# ---------- Process probing ----------


def _is_running(pid: int) -> bool:
    """Cross-platform liveness check. POSIX: signal 0. Windows: tasklist."""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False
    except AttributeError:
        # Windows os.kill semantics — fall back to subprocess query.
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=2,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False


def _read_status(shell_id: str) -> str:
    p = _shell_dir(shell_id) / "status"
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def _write_status(shell_id: str, status: str) -> None:
    try:
        (_shell_dir(shell_id) / "status").write_text(status, encoding="utf-8")
    except OSError:
        pass


def _read_pid(shell_id: str) -> int | None:
    try:
        return int((_shell_dir(shell_id) / "pid").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _refresh_status(shell_id: str) -> str:
    """Update status file based on current process state. Returns the
    refreshed status string."""
    current = _read_status(shell_id)
    if current.startswith("exited:") or current in ("killed", "timeout"):
        return current  # terminal, don't re-check
    pid = _read_pid(shell_id)
    if pid is None:
        return current
    if _is_running(pid):
        # Still running — but check wallclock budget.
        try:
            started = (_shell_dir(shell_id) / "started").read_text(encoding="utf-8").strip()
            t0 = dt.datetime.fromisoformat(started.replace("Z", "+00:00"))
            if t0.tzinfo is None:
                t0 = t0.replace(tzinfo=dt.timezone.utc)
            elapsed = (dt.datetime.now(dt.timezone.utc) - t0).total_seconds()
            if elapsed > SHELL_BG_MAX_WALLCLOCK_S:
                _kill_pid(pid)
                _write_status(shell_id, "timeout")
                return "timeout"
        except (OSError, ValueError):
            pass
        return "running"
    # Process gone — try to read its exit code from the proc file we
    # didn't keep. We don't have it; mark as exited:?.
    _write_status(shell_id, "exited:?")
    return "exited:?"


def _kill_pid(pid: int) -> None:
    """SIGTERM, wait 5s, SIGKILL if still alive."""
    try:
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        else:
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        return
    for _ in range(50):  # 5s in 100ms steps
        if not _is_running(pid):
            return
        time.sleep(0.1)
    try:
        if hasattr(os, "killpg"):
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
        else:
            os.kill(pid, signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _running_count() -> int:
    """How many shells are still alive?"""
    root = _shells_root()
    if not root.is_dir():
        return 0
    n = 0
    for d in root.iterdir():
        if not d.is_dir():
            continue
        if _refresh_status(d.name) == "running":
            n += 1
    return n


def _prune_old_shells() -> None:
    """Cap total registry size. Oldest TERMINAL-status shells dropped first."""
    root = _shells_root()
    if not root.is_dir():
        return
    entries = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        s = _read_status(d.name)
        try:
            mtime = d.stat().st_mtime
        except OSError:
            mtime = 0
        entries.append((s == "running", mtime, d))
    if len(entries) <= SHELL_BG_REGISTRY_MAX:
        return
    entries.sort(key=lambda x: (x[0], x[1]))  # running last; oldest first
    excess = len(entries) - SHELL_BG_REGISTRY_MAX
    for _, _, d in entries[:excess]:
        try:
            for f in d.iterdir():
                f.unlink(missing_ok=True)
            d.rmdir()
        except OSError:
            pass


# ---------- shell_run_bg ----------


class ShellRunBg(base.Tool):
    """Launch a shell command in the background; return a shell_id."""

    name = "shell_run_bg"
    description = (
        "Run a shell command in the BACKGROUND and return a shell_id "
        "immediately (does NOT wait for completion). Use for long-"
        "running things you want to monitor while doing other work: "
        "builds (`npm run build`), test suites, dev servers, "
        "data pipelines. Output is captured to a log file you read "
        "via `shell_output(shell_id)`. Stop with `shell_kill(shell_id)`. "
        "Limits: max " + str(SHELL_BG_MAX_RUNNING) + " concurrent, "
        "max " + str(SHELL_BG_MAX_WALLCLOCK_S // 60) + "min wallclock."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to launch.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory for the launch (default: workspace). "
                    "Path relative to workspace or absolute."
                ),
            },
            "label": {
                "type": "string",
                "description": (
                    "Optional human label so you can identify this "
                    "shell in shell_list output (e.g., 'frontend-dev-server')."
                ),
            },
        },
        "required": ["command"],
    }
    risk = "exec"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return "error: command required"

        if not approver(
            f"shell_run_bg",
            f"$ {cmd[:300]}",
            capability=("shell", "exec", cmd[:80]),
        ):
            return f"refused: shell_run_bg"

        # Auto-mode patterns apply (rm -rf /, etc.).
        # Registry.call already ran auto_aware. We're inside that approval.

        if _running_count() >= SHELL_BG_MAX_RUNNING:
            return (
                f"error: max {SHELL_BG_MAX_RUNNING} concurrent background "
                f"shells already running. Use shell_kill or wait for one "
                f"to exit."
            )

        cwd_arg = (args.get("cwd") or "").strip()
        if cwd_arg:
            cwd = Path(cwd_arg)
            if not cwd.is_absolute():
                cwd = config.WORKSPACE / cwd
        else:
            cwd = config.WORKSPACE
        if not cwd.is_dir():
            return f"error: cwd does not exist: {cwd}"

        shell_id = _new_shell_id()
        d = _shell_dir(shell_id)
        d.mkdir(parents=True, exist_ok=True)

        stdout_path = d / "stdout.log"
        stderr_path = d / "stderr.log"
        try:
            so = stdout_path.open("ab")
            se = stderr_path.open("ab")
        except OSError as e:
            return f"error: failed to open log files: {e}"

        # Cross-platform detachment: POSIX uses start_new_session,
        # Windows uses CREATE_NEW_PROCESS_GROUP so kill semantics work.
        kwargs: dict = {
            "stdout": so, "stderr": se,
            "cwd": str(cwd), "shell": True,
        }
        if hasattr(os, "setsid"):
            kwargs["start_new_session"] = True
        else:
            try:
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore
            except AttributeError:
                pass

        try:
            proc = subprocess.Popen(cmd, **kwargs)
        except (OSError, ValueError) as e:
            so.close(); se.close()
            return f"error: spawn failed: {type(e).__name__}: {e}"
        finally:
            # Don't keep file handles open in this process — the child
            # has its own FDs now via inheritance.
            try:
                so.close()
            except OSError:
                pass
            try:
                se.close()
            except OSError:
                pass

        try:
            (d / "pid").write_text(str(proc.pid), encoding="utf-8")
            (d / "cmd").write_text(cmd, encoding="utf-8")
            (d / "started").write_text(_now_iso(), encoding="utf-8")
            (d / "cwd").write_text(str(cwd), encoding="utf-8")
            label = (args.get("label") or "").strip()
            if label:
                (d / "label").write_text(label, encoding="utf-8")
            _write_status(shell_id, "running")
        except OSError:
            pass

        _prune_old_shells()
        return (
            f"shell_id: {shell_id}\n"
            f"pid: {proc.pid}\n"
            f"cmd: {cmd[:200]}\n"
            f"cwd: {cwd}\n"
            f"status: running\n"
            f"\nUse `shell_output(shell_id={shell_id!r})` to read stdout/stderr."
        )


# ---------- shell_output ----------


class ShellOutput(base.Tool):
    """Read NEW output from a background shell (stdout + stderr)."""

    name = "shell_output"
    description = (
        "Read new stdout/stderr from a background shell (since the "
        "last shell_output call for this shell_id, or the start). "
        "Returns the bytes that have arrived plus the current "
        "status (running / exited:N / killed / timeout). Cap: "
        "the latest 1 MB of buffered output; older content is dropped."
    )
    parameters = {
        "type": "object",
        "properties": {
            "shell_id": {
                "type": "string",
                "description": "Returned by shell_run_bg.",
            },
        },
        "required": ["shell_id"],
    }
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        shell_id = (args.get("shell_id") or "").strip()
        if not shell_id:
            return "error: shell_id required"
        d = _shell_dir(shell_id)
        if not d.is_dir():
            return f"error: no shell with id {shell_id!r}"

        status = _refresh_status(shell_id)
        new_stdout, new_stderr = _read_new_output(d)

        parts = [f"status: {status}"]
        if new_stdout:
            parts.append("STDOUT:")
            parts.append(new_stdout)
        if new_stderr:
            parts.append("STDERR:")
            parts.append(new_stderr)
        if not new_stdout and not new_stderr:
            parts.append("(no new output)")
        return "\n".join(parts)


def _read_new_output(d: Path) -> tuple[str, str]:
    """Return new stdout + stderr since last poll. Updates offset files."""
    return _read_with_offset(d / "stdout.log", d / "stdout.offset"), \
           _read_with_offset(d / "stderr.log", d / "stderr.offset")


def _read_with_offset(log_path: Path, offset_path: Path) -> str:
    if not log_path.is_file():
        return ""
    try:
        prev_offset = int(offset_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        prev_offset = 0
    try:
        with log_path.open("rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end <= prev_offset:
                return ""
            # Cap how much we return per call: latest SHELL_BG_MAX_OUTPUT_BYTES.
            window_start = max(prev_offset, end - SHELL_BG_MAX_OUTPUT_BYTES)
            f.seek(window_start)
            data = f.read()
        try:
            offset_path.write_text(str(end), encoding="utf-8")
        except OSError:
            pass
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


# ---------- shell_kill ----------


class ShellKill(base.Tool):
    """Terminate a background shell."""

    name = "shell_kill"
    description = (
        "Terminate a background shell launched via shell_run_bg. "
        "Sends SIGTERM, waits 5 seconds, then SIGKILL if still alive. "
        "On Windows uses TerminateProcess. Idempotent — safe to call "
        "on an already-exited shell."
    )
    parameters = {
        "type": "object",
        "properties": {
            "shell_id": {
                "type": "string",
                "description": "Returned by shell_run_bg.",
            },
        },
        "required": ["shell_id"],
    }
    risk = "exec"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        shell_id = (args.get("shell_id") or "").strip()
        if not shell_id:
            return "error: shell_id required"
        d = _shell_dir(shell_id)
        if not d.is_dir():
            return f"error: no shell with id {shell_id!r}"

        if not approver(
            f"shell_kill",
            f"shell_id={shell_id}",
            capability=("shell", "kill", shell_id),
        ):
            return f"refused: shell_kill"

        pid = _read_pid(shell_id)
        if pid is None:
            _write_status(shell_id, "killed")
            return f"shell {shell_id} had no recorded pid; marked killed"
        if not _is_running(pid):
            return f"shell {shell_id} already terminated"
        _kill_pid(pid)
        _write_status(shell_id, "killed")
        return f"shell {shell_id} (pid {pid}) killed"


# ---------- shell_list ----------


class ShellList(base.Tool):
    """List background shells the model has launched, with status."""

    name = "shell_list"
    description = (
        "List all background shells (running + recently terminated) "
        "with their pids, status, and command. Use to see what's "
        "still alive across the conversation."
    )
    parameters = {"type": "object", "properties": {}}
    risk = "read"

    def run(self, args: dict, approver: Callable[..., bool]) -> str:
        root = _shells_root()
        if not root.is_dir():
            return "(no shells yet)"
        rows: list[str] = []
        for d in sorted(root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            shell_id = d.name
            status = _refresh_status(shell_id)
            try:
                cmd = (d / "cmd").read_text(encoding="utf-8")[:80]
            except OSError:
                cmd = "?"
            try:
                label = (d / "label").read_text(encoding="utf-8")
            except OSError:
                label = ""
            tag = f" [{label}]" if label else ""
            rows.append(f"- {shell_id}{tag}  status={status}  cmd={cmd!r}")
        if not rows:
            return "(no shells yet)"
        return "\n".join(rows)
