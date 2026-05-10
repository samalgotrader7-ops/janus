"""
memory_sync.py — local-first git-backed memory sync (v1.34.1, Phase 7.2).

WHY THIS EXISTS:
Phase 7 / New differentiation. Memory cards in ~/.janus/memory/
are markdown — perfect candidates for git-based sync between
devices (laptop ↔ VPS ↔ phone-via-termux). Pre-v1.34.1 a user
who switched machines lost their accumulated context. v1.34.1
ships `janus sync push/pull/init/status` — plain git subprocess
wrappers around ~/.janus/memory/.

DESIGN — DELIBERATELY THIN:
We don't reimplement git. The user's `git` binary does the work;
we shell out and check exit codes. This means:
  * Authentication is git's: SSH keys, credential helpers, etc.
  * Conflict resolution is git's: rebase / merge as the user prefers.
  * Hosting is anywhere git works: GitHub, GitLab, Gitea, raw SSH.
  * Encryption-at-rest is the user's: git-crypt / age / etc.

Janus doesn't add a sync protocol; we just reuse the most
ubiquitous distributed VCS on earth.

CONFLICT-FREE BY CONVENTION:
Memories are append-only by default. The cli_rich /memory review
flow proposes diffs the user reviews and accepts; once accepted
the file is rarely re-edited. Multiple devices that all only
APPEND don't conflict in practice. When edits happen, git's
merge / rebase handles them like any markdown file.

USAGE:
  janus sync init git@github.com:user/janus-memory.git
    # initializes ~/.janus/memory/ as a git repo + adds remote

  janus sync push        # commits + pushes local changes
  janus sync pull        # pulls + auto-rebases remote changes
  janus sync status      # shows what's local-only / remote-only

P5: ~/.janus/memory/.git/ is just git's normal directory. The
user can `cd ~/.janus/memory && git ...` directly any time.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    message: str
    detail: str = ""


# ---------- Helpers ----------


def _memory_dir() -> Path:
    return Path(config.HOME) / "memory"


def _have_git() -> bool:
    return shutil.which("git") is not None


def _is_git_repo(path: Path) -> bool:
    return (path / ".git").exists() or (path / ".git").is_dir()


def _run_git(
    *args: str,
    cwd: Path | None = None,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Wrapper. Returns CompletedProcess; never raises (we inspect
    returncode + stderr in callers)."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


# ---------- Public API ----------


def init(remote_url: str) -> SyncResult:
    """Initialize ~/.janus/memory/ as a git repo with `remote_url`
    as origin. Idempotent — re-running with a different URL
    updates the remote."""
    if not _have_git():
        return SyncResult(False, "git binary not found on PATH")
    mem = _memory_dir()
    if not mem.exists():
        return SyncResult(
            False,
            f"memory directory not found at {mem}",
            "create some memory cards first (e.g. with /memory) "
            "or `mkdir ~/.janus/memory`",
        )
    if not _is_git_repo(mem):
        r = _run_git("init", "-b", "main", cwd=mem)
        if r.returncode != 0:
            return SyncResult(False, "git init failed", r.stderr.strip())
    # Set or update the remote.
    r = _run_git("remote", "set-url", "origin", remote_url, cwd=mem)
    if r.returncode != 0:
        # remote doesn't exist yet — add it.
        r = _run_git("remote", "add", "origin", remote_url, cwd=mem)
        if r.returncode != 0:
            return SyncResult(False, "git remote add failed", r.stderr.strip())
    return SyncResult(True, f"initialized {mem} with origin → {remote_url}")


def push(commit_message: str | None = None) -> SyncResult:
    """Stage all changes, commit (if any), push to origin/main.
    No-op if working tree is clean."""
    if not _have_git():
        return SyncResult(False, "git binary not found on PATH")
    mem = _memory_dir()
    if not _is_git_repo(mem):
        return SyncResult(
            False,
            f"{mem} is not a git repo",
            "run `janus sync init <remote>` first",
        )
    # Stage all changes.
    r = _run_git("add", "-A", cwd=mem)
    if r.returncode != 0:
        return SyncResult(False, "git add failed", r.stderr.strip())
    # Commit (or no-op when nothing changed).
    msg = commit_message or "janus sync"
    r = _run_git("commit", "-m", msg, cwd=mem)
    # commit returns 1 when there's nothing to commit — that's not an
    # error for us, just means the working tree was clean.
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr).lower():
        return SyncResult(False, "git commit failed", (r.stderr or r.stdout).strip())
    # Push.
    r = _run_git("push", "-u", "origin", "main", cwd=mem)
    if r.returncode != 0:
        return SyncResult(False, "git push failed", r.stderr.strip())
    return SyncResult(True, "pushed to origin/main")


def pull(*, rebase: bool = True) -> SyncResult:
    """Pull origin/main. Auto-rebases by default to keep history
    linear. Conflicts (rare) require manual `git` work."""
    if not _have_git():
        return SyncResult(False, "git binary not found on PATH")
    mem = _memory_dir()
    if not _is_git_repo(mem):
        return SyncResult(
            False,
            f"{mem} is not a git repo",
            "run `janus sync init <remote>` first",
        )
    args = ["pull", "--rebase" if rebase else "--ff-only", "origin", "main"]
    r = _run_git(*args, cwd=mem)
    if r.returncode != 0:
        return SyncResult(
            False,
            "git pull failed",
            (r.stderr or r.stdout).strip(),
        )
    return SyncResult(True, "pulled from origin/main")


def status() -> SyncResult:
    """Show working-tree + branch status relative to origin."""
    if not _have_git():
        return SyncResult(False, "git binary not found on PATH")
    mem = _memory_dir()
    if not _is_git_repo(mem):
        return SyncResult(False, f"{mem} is not a git repo")
    # Compact status.
    r = _run_git("status", "--short", "--branch", cwd=mem)
    if r.returncode != 0:
        return SyncResult(False, "git status failed", r.stderr.strip())
    return SyncResult(True, "ok", (r.stdout or "").rstrip())


# ---------- CLI dispatch ----------


def cmd_sync(args: list[str]) -> int:
    """`janus sync {init <url> | push [-m MSG] | pull | status}`"""
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(
            "usage: janus sync {init <remote> | push [-m MSG] | pull | status}\n"
            "  Git-backed sync of ~/.janus/memory/ across devices.\n"
            "  Bring your own remote (GitHub / GitLab / Gitea / raw SSH).\n"
        )
        return 0 if args else 2

    sub = args[0]
    rest = args[1:]
    result: SyncResult

    if sub == "init":
        if not rest:
            sys.stderr.write("error: usage: janus sync init <remote-url>\n")
            return 2
        result = init(rest[0])
    elif sub == "push":
        msg = None
        if rest and rest[0] in ("-m", "--message"):
            try:
                msg = rest[1]
            except IndexError:
                sys.stderr.write("error: -m requires a message\n")
                return 2
        result = push(commit_message=msg)
    elif sub == "pull":
        rebase = "--ff-only" not in rest
        result = pull(rebase=rebase)
    elif sub == "status":
        result = status()
    else:
        sys.stderr.write(f"error: unknown subcommand {sub!r}\n")
        sys.stderr.write("usage: janus sync {init|push|pull|status}\n")
        return 2

    if result.ok:
        sys.stdout.write(f"{result.message}\n")
        if result.detail:
            sys.stdout.write(result.detail + "\n")
        return 0
    sys.stderr.write(f"error: {result.message}\n")
    if result.detail:
        sys.stderr.write(result.detail + "\n")
    return 1
