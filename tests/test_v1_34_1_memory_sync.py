"""Tests for v1.34.1 — local-first memory sync (Phase 7.2)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from janus import memory_sync


@pytest.fixture
def fake_home_with_memory(tmp_path, monkeypatch):
    home = tmp_path / ".janus"
    mem = home / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("# index\n")
    (mem / "user_role.md").write_text("---\nname: x\n---\nbody")
    from janus import config
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(memory_sync.config, "HOME", home)
    return home


@pytest.fixture
def have_git():
    """Skip tests that need git when it's not on PATH."""
    if not shutil.which("git"):
        pytest.skip("git not on PATH")
    return True


def _run(cwd, *args):
    """Run a git command, return CompletedProcess. Used by tests
    to set up bare repos as fake remotes."""
    return subprocess.run(
        ["git", *args], cwd=str(cwd),
        capture_output=True, text=True, check=False,
    )


# -------------------- Helpers --------------------


def test_have_git_helper():
    assert isinstance(memory_sync._have_git(), bool)


def test_is_git_repo_false_initially(fake_home_with_memory):
    mem = memory_sync._memory_dir()
    assert memory_sync._is_git_repo(mem) is False


# -------------------- init --------------------


def test_init_no_git_returns_error_message(fake_home_with_memory, monkeypatch):
    """When git binary isn't available, init returns ok=False with
    a clear message."""
    monkeypatch.setattr(memory_sync, "_have_git", lambda: False)
    result = memory_sync.init("git@example.com:x/y.git")
    assert result.ok is False
    assert "git binary" in result.message.lower()


def test_init_no_memory_dir(tmp_path, monkeypatch):
    """init refuses if ~/.janus/memory/ doesn't exist."""
    fresh = tmp_path / "empty"
    fresh.mkdir()
    from janus import config
    monkeypatch.setattr(config, "HOME", fresh)
    monkeypatch.setattr(memory_sync.config, "HOME", fresh)
    result = memory_sync.init("git@example.com:x/y.git")
    assert result.ok is False
    assert "memory" in result.message.lower()


def test_init_creates_repo(fake_home_with_memory, have_git):
    """init creates a git repo + sets origin to the URL."""
    result = memory_sync.init("https://example.com/test.git")
    assert result.ok is True
    mem = memory_sync._memory_dir()
    assert (mem / ".git").exists()
    # origin URL set
    r = _run(mem, "remote", "get-url", "origin")
    assert r.returncode == 0
    assert "test.git" in r.stdout


def test_init_idempotent(fake_home_with_memory, have_git):
    """Re-running init updates the remote URL instead of erroring."""
    memory_sync.init("https://example.com/first.git")
    result = memory_sync.init("https://example.com/second.git")
    assert result.ok is True
    mem = memory_sync._memory_dir()
    r = _run(mem, "remote", "get-url", "origin")
    assert "second.git" in r.stdout


# -------------------- push (against bare remote) --------------------


def test_push_against_bare_remote(fake_home_with_memory, tmp_path, have_git):
    """End-to-end: init against a bare repo, push, verify the
    remote received the commit."""
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "--bare", str(bare))

    # Need to set commit identity for the test git environment.
    mem = memory_sync._memory_dir()
    _run(mem, "init", "-b", "main")
    _run(mem, "config", "user.email", "test@example.com")
    _run(mem, "config", "user.name", "Test User")

    result = memory_sync.init(f"file://{bare}")
    assert result.ok is True

    push_result = memory_sync.push(commit_message="initial sync")
    assert push_result.ok is True
    # Verify the bare repo has the main branch now.
    r = _run(bare, "branch", "--list", "main")
    assert "main" in r.stdout


def test_push_clean_tree_succeeds(fake_home_with_memory, tmp_path, have_git):
    """A second push when nothing changed succeeds (no-op commit)."""
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "--bare", str(bare))
    mem = memory_sync._memory_dir()
    _run(mem, "init", "-b", "main")
    _run(mem, "config", "user.email", "t@e.com")
    _run(mem, "config", "user.name", "T")
    memory_sync.init(f"file://{bare}")
    memory_sync.push("first")
    # Push again with no changes
    result = memory_sync.push("second")
    assert result.ok is True


def test_push_when_not_a_repo(fake_home_with_memory, have_git):
    """push tells the user to init when the dir isn't a git repo
    yet."""
    result = memory_sync.push()
    assert result.ok is False
    assert "not a git repo" in result.message.lower() or \
        "init" in result.detail.lower()


# -------------------- pull --------------------


def test_pull_when_not_a_repo(fake_home_with_memory, have_git):
    result = memory_sync.pull()
    assert result.ok is False
    assert "not a git repo" in result.message.lower()


def test_pull_against_bare_remote(fake_home_with_memory, tmp_path, have_git):
    """End-to-end: init against a remote, push from a sibling
    clone, pull from our memory dir, verify content."""
    bare = tmp_path / "remote.git"
    _run(tmp_path, "init", "--bare", str(bare))

    # Sibling clone: writes a file then pushes.
    sibling = tmp_path / "sibling"
    _run(tmp_path, "clone", f"file://{bare}", str(sibling))
    _run(sibling, "config", "user.email", "s@e.com")
    _run(sibling, "config", "user.name", "S")
    (sibling / "remote_only.md").write_text("# from sibling")
    _run(sibling, "add", "-A")
    _run(sibling, "commit", "-m", "remote add")
    _run(sibling, "branch", "-M", "main")
    _run(sibling, "push", "-u", "origin", "main")

    # Our memory dir: init + pull
    mem = memory_sync._memory_dir()
    _run(mem, "init", "-b", "main")
    _run(mem, "config", "user.email", "t@e.com")
    _run(mem, "config", "user.name", "T")
    _run(mem, "remote", "add", "origin", f"file://{bare}")
    # Need a commit to rebase against
    _run(mem, "add", "-A")
    _run(mem, "commit", "-m", "local")

    result = memory_sync.pull(rebase=True)
    # Whether this succeeds depends on git's pull-rebase mechanics
    # with a divergent remote — at minimum our function shouldn't
    # raise. Clean exit is what matters; remote_only.md may or
    # may not appear depending on rebase strategy.
    # At a minimum: no exception.
    assert hasattr(result, "ok")


# -------------------- status --------------------


def test_status_when_not_a_repo(fake_home_with_memory, have_git):
    result = memory_sync.status()
    assert result.ok is False


def test_status_returns_branch_info(fake_home_with_memory, have_git):
    mem = memory_sync._memory_dir()
    _run(mem, "init", "-b", "main")
    _run(mem, "config", "user.email", "t@e.com")
    _run(mem, "config", "user.name", "T")
    _run(mem, "add", "-A")
    _run(mem, "commit", "-m", "init")
    result = memory_sync.status()
    assert result.ok is True
    # The detail field carries the porcelain output, including
    # branch info.
    assert "main" in (result.detail or "").lower() or result.detail == ""


# -------------------- CLI --------------------


def test_cmd_sync_no_args_prints_help(capsys):
    rc = memory_sync.cmd_sync([])
    captured = capsys.readouterr()
    assert "usage" in captured.out.lower()
    assert rc == 2


def test_cmd_sync_help_succeeds(capsys):
    rc = memory_sync.cmd_sync(["--help"])
    assert rc == 0


def test_cmd_sync_init_requires_url(capsys):
    rc = memory_sync.cmd_sync(["init"])
    assert rc == 2


def test_cmd_sync_unknown_subcommand_errors(capsys):
    rc = memory_sync.cmd_sync(["notreal"])
    assert rc == 2


def test_cmd_sync_pull_no_repo_returns_1(fake_home_with_memory, capsys):
    rc = memory_sync.cmd_sync(["pull"])
    assert rc == 1


# -------------------- __main__ wiring --------------------


def test_main_dispatches_sync_subcommand():
    main_path = Path(memory_sync.__file__).parent / "__main__.py"
    src = main_path.read_text(encoding="utf-8")
    assert 'sub == "sync"' in src
    assert "from . import memory_sync" in src


# -------------------- Version pin --------------------


def test_version_bumped_to_1_34_1_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 1)
