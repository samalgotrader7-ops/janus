"""Tests for v1.32.2 — one-line installer (Phase 5.3).

WHAT THIS SHIPS:
scripts/install.sh — the one-liner users curl-pipe to install Janus.

USAGE:
  curl -sSL .../scripts/install.sh | sh

DESIGN INVARIANTS PINNED:
  * POSIX-compatible (works under /bin/sh, dash, bash, zsh)
  * Detects platform (Linux / macOS); refuses Windows with hint
  * Requires Python 3.10+
  * Installs pipx if missing (--user, no sudo)
  * Auto-detects install source: tries PyPI, falls back to git+URL
    on failure (so Phase 5.1's dormant-PyPI doesn't break installs)
  * Honors JANUS_INSTALL_SOURCE / JANUS_INSTALL_REF /
    JANUS_INSTALL_EXTRAS env overrides
  * Prints next-step instructions (env vars + onboard + gateways)
  * No interactivity (safe to pipe to sh)
"""

from __future__ import annotations

from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "install.sh"
)


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


# -------------------- File-level pins --------------------


def test_installer_exists():
    assert SCRIPT_PATH.exists()


def test_installer_uses_posix_shebang(script_text):
    """`#!/usr/bin/env sh` so curl-pipe to /bin/sh, dash, or bash all
    work. Bash-specific syntax inside would break dash users."""
    first_line = script_text.splitlines()[0]
    assert first_line.startswith("#!")
    assert "/sh" in first_line or "env sh" in first_line


def test_installer_uses_strict_mode(script_text):
    """`set -eu` — POSIX-portable strict mode. -o pipefail is
    bash-only so we skip it (intentionally; docs note this)."""
    assert "set -eu" in script_text


def test_installer_no_bashisms(script_text):
    """Spot-check for accidental bashisms that would break under
    /bin/sh (dash). [[ ]] is the most common offender."""
    # Strip comments before scanning
    body_lines = [
        line for line in script_text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    body = "\n".join(body_lines)
    assert "[[ " not in body, "Found [[ bashism — use [ ] for POSIX"
    # &> is bash, >& is POSIX
    assert "&>" not in body or ">/dev/null" in body


# -------------------- Platform / Python detection --------------------


def test_installer_detects_linux_and_macos(script_text):
    """uname-based platform check; both Linux and Darwin (macOS)
    handled. Other platforms should error out cleanly."""
    assert 'OS="$(uname -s)"' in script_text or "OS=\"$(uname -s)\"" in script_text
    assert "Linux" in script_text
    assert "Darwin" in script_text


def test_installer_refuses_windows_with_hint(script_text):
    """Windows users get a clear pointer to WSL or pipx — not just
    a confusing failure mid-install."""
    assert "Unsupported platform" in script_text
    assert "WSL" in script_text or "Windows" in script_text


def test_installer_checks_python_version(script_text):
    """Janus needs Python 3.10+. Catch this before pipx tries to
    create a venv with an older interpreter."""
    assert "python3" in script_text
    assert "3.10" in script_text
    # Major / minor version probe
    assert "version_info" in script_text


# -------------------- pipx setup --------------------


def test_installer_installs_pipx_if_missing(script_text):
    """If pipx is missing, install via `pip install --user pipx`
    + `pipx ensurepath`. No sudo required."""
    assert "command -v pipx" in script_text
    assert "pip install" in script_text and "pipx" in script_text
    assert "pipx ensurepath" in script_text


def test_installer_handles_pep668_break_system_packages(script_text):
    """Recent Debian/Ubuntu (PEP 668) refuse system-level pip
    installs without --break-system-packages. The bootstrap step
    needs that flag — once pipx is installed, it manages its own
    venvs and the flag is irrelevant."""
    assert "--break-system-packages" in script_text


def test_installer_refreshes_path_for_local_bin(script_text):
    """After pipx install, ~/.local/bin must be on PATH for the
    rest of the script to find pipx + janus. Refresh in-process."""
    assert "PATH" in script_text
    assert "USER_BASE" in script_text or ".local/bin" in script_text


# -------------------- Install source --------------------


def test_installer_supports_pypi_install(script_text):
    """Tries PyPI first (auto mode) — once Phase 5.1's secret is
    configured and the namespace is reserved, this becomes the
    default fast path."""
    assert "janus-agent" in script_text
    assert "PYPI_SPEC" in script_text or "install_from_pypi" in script_text


def test_installer_falls_back_to_git_url(script_text):
    """When PyPI install fails (e.g. namespace not yet reserved),
    fall back to git+https://github.com/.../janus.git so users
    aren't blocked on PyPI publish setup."""
    assert "git+https://github.com/samalgotrader7-ops/janus.git" in script_text
    assert "install_from_git" in script_text or "GIT_SPEC" in script_text


def test_installer_honors_install_source_override(script_text):
    """JANUS_INSTALL_SOURCE=pypi|git lets advanced users force the
    source — useful for testing the PyPI workflow before flipping
    the default."""
    assert "JANUS_INSTALL_SOURCE" in script_text
    # Both override branches present
    assert '"pypi"' in script_text or "pypi)" in script_text
    assert '"git"' in script_text or "git)" in script_text


def test_installer_honors_install_ref_override(script_text):
    """JANUS_INSTALL_REF=v1.32.1 lets users pin to a specific tag
    when using the git source. Default 'main' for HEAD-of-main."""
    assert "JANUS_INSTALL_REF" in script_text
    assert 'main' in script_text


def test_installer_uses_extras_all_by_default(script_text):
    """Extras 'all' bundles web + telegram + browser + tui + rich
    so any subcommand works post-install. Override with
    JANUS_INSTALL_EXTRAS=rich (etc.) to slim down."""
    assert "JANUS_INSTALL_EXTRAS" in script_text
    assert "all" in script_text


# -------------------- Post-install UX --------------------


def test_installer_prints_next_steps(script_text):
    """User who pipes-curl to sh has no obvious next move. Final
    block tells them: set env vars, run `janus onboard`, optional
    gateways."""
    assert "JANUS_API_KEY" in script_text
    assert "janus onboard" in script_text


def test_installer_mentions_install_services_for_vps(script_text):
    """Crucial for VPS users — mentions the systemd deployment
    script as the production path."""
    assert "install_services.sh" in script_text


def test_installer_no_interactive_prompts(script_text):
    """Safe to pipe to sh: no `read` calls, no interactive
    confirmation. The user already chose to install when they
    typed the curl command."""
    # Look for `read ` (POSIX read builtin) — should not appear
    body_lines = [
        line for line in script_text.splitlines()
        if not line.lstrip().startswith("#")
    ]
    body = "\n".join(body_lines)
    # Allow `command -v read` style if it appears (unlikely)
    assert "read -r" not in body
    assert "read REPLY" not in body
    # No `read ` followed by a variable name
    import re
    assert not re.search(r'\bread\s+[A-Z_]+\b', body), (
        "Found `read VAR` — would block when piped to sh"
    )


# -------------------- Version pin --------------------


def test_version_bumped_to_1_32_2_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 32, 2)
