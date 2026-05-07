"""
shell_pty.py — v1.24.1 PTY-backed background shells.

PROBLEM:
The v1.15 ShellRunBg captures subprocess stdout/stderr to log files.
That works for non-interactive commands but breaks any program that:
  * Reads from stdin (read, prompt, fzf, less)
  * Detects terminal type via isatty() (most CLIs change formatting
    when stdin/stdout aren't a TTY)
  * Uses curses / ncurses / Textual / Rich live regions
  * Sends ANSI cursor positioning that depends on terminal size

PTY shells fix this by allocating a pseudo-terminal so the child
process sees a real TTY. Combined with the v1.24.0 SSE output stream
+ xterm.js viewer + this module's stdin endpoint, the user gets a
fully interactive terminal experience in the browser.

PORTABILITY:
POSIX: uses the built-in `pty` module (pty.openpty + os.fork via
       subprocess.Popen with the slave fd).
Windows: not supported in v1.24.1. Calls fail with NotImplementedError
         and the web /api/shells/run endpoint refuses pty=True on
         Windows with a clear message. Adding ConPTY support
         (Python 3.13+) is a v1.24.2 follow-up.

LIFECYCLE:
A PTY shell shares the same on-disk layout as ShellRunBg's:
  ~/.janus/shells/<id>/
    cmd         — the command line
    pid         — child PID
    started     — ISO timestamp
    status      — 'running' | 'exited' | 'killed' | 'failed'
    stdout.log  — combined PTY output (stderr is multiplexed into the
                  same stream because most TTY apps don't differentiate)
    pty_master  — marker file (presence ⇒ PTY shell, write_stdin ok)

A reader thread copies bytes from the PTY master fd into stdout.log
so the existing /api/shells/<id>/stream endpoint can SSE-tail it
unchanged.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from .. import config


def is_supported() -> bool:
    """v1.24.1 supports PTY only on POSIX. Windows lands in v1.24.2."""
    return os.name == "posix"


# Module-level table: shell_id → master fd. Used by write_stdin to
# route bytes into the right PTY without re-opening anything.
_master_fds: dict[str, int] = {}
_master_lock = threading.Lock()


def _shell_dir(shell_id: str) -> Path:
    return config.HOME / "shells" / shell_id


def _new_shell_id() -> str:
    """Same id shape as shell_bg uses: sh-<8-hex>."""
    import secrets
    return "sh-" + secrets.token_hex(4)


def start_pty_shell(command: str, cwd: Path | None = None) -> str:
    """Spawn a child process under a fresh PTY. Returns the shell_id.

    Raises NotImplementedError on Windows. Use is_supported() to gate.
    """
    if not is_supported():
        raise NotImplementedError(
            "PTY shells require POSIX. Windows ConPTY support is a "
            "v1.24.2 follow-up. Use ShellRunBg (no PTY) on Windows."
        )
    import pty
    import subprocess

    shell_id = _new_shell_id()
    d = _shell_dir(shell_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "cmd").write_text(command, encoding="utf-8")
    (d / "started").write_text(_now_iso(), encoding="utf-8")
    (d / "status").write_text("running", encoding="utf-8")
    (d / "pty_master").write_text("1", encoding="utf-8")

    master_fd, slave_fd = pty.openpty()
    # Set TERM so child apps recognize the pty as a colour-capable terminal.
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")

    # Spawn the child. argv[0]=sh -c keeps shell semantics so users can
    # pass pipelines, env-vars, etc., the same way as ShellRunBg.
    try:
        proc = subprocess.Popen(
            ["/bin/sh", "-c", command],
            stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
            cwd=str(cwd or config.WORKSPACE),
            env=env,
            close_fds=True,
            preexec_fn=os.setsid,  # new session ⇒ kill propagates to group
        )
    except Exception as e:
        os.close(master_fd)
        os.close(slave_fd)
        (d / "status").write_text("failed", encoding="utf-8")
        (d / "stdout.log").write_text(
            f"[error launching pty shell: {e}]\n", encoding="utf-8",
        )
        raise

    os.close(slave_fd)
    (d / "pid").write_text(str(proc.pid), encoding="utf-8")

    with _master_lock:
        _master_fds[shell_id] = master_fd

    threading.Thread(
        target=_reader_loop, args=(shell_id, proc, master_fd, d),
        daemon=True,
    ).start()
    return shell_id


def _reader_loop(shell_id: str, proc, master_fd: int, d: Path) -> None:
    """Tail the PTY master fd into stdout.log until child exits."""
    log_path = d / "stdout.log"
    try:
        # Open the log in append + binary so we capture exactly what the
        # PTY emits (xterm.js handles encoding).
        with log_path.open("ab", buffering=0) as out:
            while True:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                out.write(chunk)
        rc = proc.wait()
        status = "exited" if rc == 0 else "failed"
        (d / "status").write_text(status, encoding="utf-8")
        (d / "exit_code").write_text(str(rc), encoding="utf-8")
    except Exception as e:
        (d / "status").write_text("failed", encoding="utf-8")
        try:
            with log_path.open("ab") as out:
                out.write(f"\n[reader_loop error: {e}]\n".encode("utf-8"))
        except OSError:
            pass
    finally:
        with _master_lock:
            fd = _master_fds.pop(shell_id, None)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def write_stdin(shell_id: str, data: str) -> int:
    """Send `data` to the shell's stdin. Returns bytes written.

    Raises:
        ValueError: shell_id has no PTY master (not a PTY shell, exited,
                    or unknown).
    """
    if not is_supported():
        raise NotImplementedError("PTY stdin requires POSIX")
    payload = data.encode("utf-8")
    with _master_lock:
        fd = _master_fds.get(shell_id)
    if fd is None:
        raise ValueError(
            f"no PTY master for {shell_id} "
            f"(not a PTY shell, exited, or unknown)"
        )
    return os.write(fd, payload)


def is_pty_shell(shell_id: str) -> bool:
    """Did this shell_id start with PTY mode?"""
    return (_shell_dir(shell_id) / "pty_master").is_file()


def kill_pty_shell(shell_id: str) -> bool:
    """Send SIGTERM to the child's process group. Returns True on success."""
    if not is_supported():
        return False
    pid_path = _shell_dir(shell_id) / "pid"
    if not pid_path.is_file():
        return False
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return False
    try:
        import signal
        os.killpg(os.getpgid(pid), signal.SIGTERM)
        # Update status optimistically — reader_loop will overwrite if
        # it observes a different exit.
        (_shell_dir(shell_id) / "status").write_text(
            "killed", encoding="utf-8",
        )
        return True
    except (OSError, ProcessLookupError):
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
