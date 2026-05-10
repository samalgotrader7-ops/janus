"""Source-pin tests for scripts/git-hooks/post-merge auto-restart hook.

WHY THIS EXISTS:
After Sam migrated his VPS to systemd-managed services (v1.31.17),
he wanted code-pull → restart to be automatic. Two approaches:
  - tracked git hook + core.hooksPath (this one)
  - systemd path unit watching .git/refs/heads/main

We picked the git hook because the workflow is simple, debuggable
(it's just a shell script), and matches "Sam pulls on the VPS"
exactly — the only path where auto-restart is needed.

INVARIANTS PINNED:
  * Hook file exists at scripts/git-hooks/post-merge
  * Has bash shebang
  * Uses set -euo pipefail
  * Restarts all three janus-* services
  * Conditional on janus/*.py changes (skips doc-only / test-only pulls)
  * Honors JANUS_NO_AUTO_RESTART=1 bypass env var
  * Bails silently when systemd isn't available
  * Bails silently when ORIG_HEAD doesn't exist
  * Warns on pyproject.toml changes (deps may need pipx reinstall)
  * install_services.sh wires git config core.hooksPath
  * install_services.sh chmods the hook executable
"""

from __future__ import annotations

from pathlib import Path

import pytest


HOOK_PATH = (
    Path(__file__).parent.parent / "scripts" / "git-hooks" / "post-merge"
)
INSTALL_PATH = (
    Path(__file__).parent.parent / "scripts" / "install_services.sh"
)


@pytest.fixture(scope="module")
def hook_text() -> str:
    return HOOK_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def install_text() -> str:
    return INSTALL_PATH.read_text(encoding="utf-8")


# ---------- hook itself ----------


def test_hook_exists():
    assert HOOK_PATH.exists()
    assert HOOK_PATH.is_file()


def test_hook_has_bash_shebang(hook_text):
    """Use bash explicitly — we use [[ ]], $'\\033...', and other
    bash-isms that break on /bin/sh (dash on Debian/Ubuntu)."""
    first_line = hook_text.splitlines()[0]
    assert first_line.startswith("#!")
    assert "bash" in first_line


def test_hook_uses_strict_mode(hook_text):
    """Standard fail-loudly posture so a failed restart is visible
    in the git pull output."""
    assert "set -euo pipefail" in hook_text


def test_hook_restarts_all_three_services(hook_text):
    """janus-telegram + janus-web + janus-daemon all restart on a
    qualifying pull. Iteration order doesn't matter; pin presence."""
    for svc in ("janus-telegram", "janus-web", "janus-daemon"):
        assert svc in hook_text


def test_hook_uses_systemctl_user(hook_text):
    """User-mode systemd, matching install_services.sh setup."""
    assert "systemctl --user restart" in hook_text


def test_hook_skips_when_no_python_changed(hook_text):
    """Doc / script / test pulls don't bounce the services. The
    early-return checks for janus/*.py via grep -E."""
    # Must reference janus/*.py pattern in the change detection.
    assert "janus/.*\\.py$" in hook_text or 'janus/.*\\.py' in hook_text
    # And must have an early-return path with no restart
    assert "no janus/*.py changes" in hook_text or "exit 0" in hook_text


def test_hook_honors_no_auto_restart_env_var(hook_text):
    """JANUS_NO_AUTO_RESTART=1 lets Sam pull-without-restart when
    he explicitly wants that (e.g. inspect new code first)."""
    assert "JANUS_NO_AUTO_RESTART" in hook_text
    # The check should exit early (before any restart logic).
    bypass_idx = hook_text.index("JANUS_NO_AUTO_RESTART")
    restart_idx = hook_text.index("systemctl --user restart")
    assert bypass_idx < restart_idx


def test_hook_bails_silently_without_systemd(hook_text):
    """Local dev clones (no systemd, or systemd-less platforms like
    macOS) get the hook from the tracked path but should silently
    no-op — no error message, no fake restart attempt."""
    assert 'command -v systemctl' in hook_text
    assert "is-system-running" in hook_text


def test_hook_handles_missing_orig_head(hook_text):
    """ORIG_HEAD is set by git pull/merge but not by all hook
    invocations. Bail silently if it's not there instead of
    crashing the pull."""
    assert "ORIG_HEAD" in hook_text
    # Specifically, the verify-or-exit pattern
    assert "rev-parse --verify ORIG_HEAD" in hook_text


def test_hook_warns_on_pyproject_change(hook_text):
    """pyproject.toml changes mean dependency changes — restart
    alone won't pull new packages. Warn but don't auto-run pipx
    (which can fail in subtle ways without a watching user)."""
    assert "pyproject.toml" in hook_text
    assert "pipx reinstall janus-agent" in hook_text


def test_hook_uses_diff_with_orig_head(hook_text):
    """File-list comes from `git diff --name-only ORIG_HEAD HEAD`
    — the canonical post-merge diff. Pin so a future edit doesn't
    accidentally switch to a working-tree diff (which would include
    unstaged changes)."""
    assert "git diff --name-only ORIG_HEAD HEAD" in hook_text


# ---------- install_services.sh wires the hook ----------


def test_install_script_sets_core_hooks_path(install_text):
    """The wiring step runs `git config core.hooksPath
    scripts/git-hooks` so the tracked hook fires from the standard
    git hook machinery."""
    assert 'git config core.hooksPath "scripts/git-hooks"' in install_text


def test_install_script_chmods_hook_executable(install_text):
    """Git tracks the executable bit, but a fresh clone via certain
    Windows toolchains can lose it. Belt-and-suspenders chmod +x
    in the install script ensures the hook can actually run."""
    assert "chmod +x" in install_text
    # And specifically the hook file
    assert "post-merge" in install_text


def test_install_script_documents_bypass_in_closing_notes(install_text):
    """Final printout tells the user how to skip restart on a
    pull when they want to."""
    assert "JANUS_NO_AUTO_RESTART" in install_text
    # Mentioned in the user-visible printout, not just internally
    notes_idx = install_text.index("After-pull is now AUTOMATIC")
    assert notes_idx > 0
