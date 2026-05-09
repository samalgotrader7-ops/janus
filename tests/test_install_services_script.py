"""Source-pin tests for scripts/install_services.sh.

WHY THESE EXIST:
The install script is the documented one-shot deployment path for
Sam's VPS (added 2026-05-09 alongside v1.31.17). It's a shell
script, not python — but we still want to pin its key safety
properties so a future edit doesn't accidentally drop strict-mode
or skip linger setup.

INVARIANTS PINNED:
  * File exists at scripts/install_services.sh
  * Has bash shebang at line 1 (not /bin/sh — uses bash arrays)
  * Uses set -euo pipefail (fails fast)
  * Validates required env vars before writing .env
  * Stops nohup processes before installing systemd units
  * Calls `janus service install --force` (so v1.31.17 web unit
    actually replaces older 2-unit installs)
  * Sets restrictive perms on .env (chmod 600)
  * Enables loginctl linger (so user-systemd survives logout)
"""

from __future__ import annotations

from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).parent.parent / "scripts" / "install_services.sh"
)


@pytest.fixture(scope="module")
def script_text() -> str:
    return SCRIPT_PATH.read_text(encoding="utf-8")


def test_script_exists():
    assert SCRIPT_PATH.exists()
    assert SCRIPT_PATH.is_file()


def test_script_has_bash_shebang(script_text):
    """First line should be a bash shebang. We use bash arrays +
    associative arrays — /bin/sh would break on Debian/Ubuntu where
    sh is dash."""
    first_line = script_text.splitlines()[0]
    assert first_line.startswith("#!")
    assert "bash" in first_line


def test_script_uses_strict_mode(script_text):
    """``set -euo pipefail`` is the standard 'fail loudly' shell
    posture. -e exits on any error, -u catches unset vars,
    -o pipefail surfaces failures inside pipes."""
    assert "set -euo pipefail" in script_text


def test_script_validates_required_env_vars(script_text):
    """Required vars: JANUS_API_KEY / API_BASE / MODEL. Missing any
    of those should error out with a hint, not silently skip."""
    assert "REQUIRED_VARS=" in script_text
    assert "JANUS_API_KEY" in script_text
    assert "JANUS_API_BASE" in script_text
    assert "JANUS_MODEL" in script_text
    # Has explicit missing-var error path
    assert "Required env vars missing" in script_text


def test_script_stops_nohup_processes_before_install(script_text):
    """If nohup-launched processes are still running when systemd
    starts its own, port conflicts / Telegram getUpdates conflicts
    follow. Stop step must come before the install step."""
    stop_idx = script_text.index("Stopping any nohup-launched")
    install_idx = script_text.index("Installing janus systemd units")
    assert stop_idx < install_idx


def test_script_uses_force_install(script_text):
    """``janus service install --force`` is required when upgrading
    from a pre-v1.31.17 install (which only had 2 units) so the new
    janus-web.service actually appears."""
    assert "janus service install --force" in script_text


def test_script_chmods_env_file_to_600(script_text):
    """Env file contains JANUS_API_KEY + JANUS_TELEGRAM_TOKEN —
    both are credentials. chmod 600 keeps them out of any
    other-user-readable paths."""
    assert 'chmod 600 "$ENV_FILE"' in script_text


def test_script_enables_linger(script_text):
    """Without linger, user-systemd shuts down on logout, killing
    all janus services on SSH disconnect. linger is REQUIRED for
    headless VPS deployments."""
    assert "loginctl enable-linger" in script_text


def test_script_idempotent_marker_documented(script_text):
    """The script should self-document that it's safe to re-run
    (idempotent). A user who runs it twice shouldn't lose env-file
    edits they made between runs."""
    assert "IDEMPOTENT" in script_text or "idempotent" in script_text.lower()


def test_script_documents_force_env_flag(script_text):
    """FORCE_ENV=1 is the escape hatch that overwrites .env with
    shell values. Document it so users know how to refresh after
    rotating credentials."""
    assert "FORCE_ENV" in script_text


def test_script_has_final_status_check(script_text):
    """After install, the script reports active/inactive state for
    each service so the user sees immediately if something's wrong."""
    assert "Final status" in script_text
    assert "is-active" in script_text


def test_script_documents_ssh_tunnel_for_localhost_web(script_text):
    """Default web bind is 127.0.0.1 — the script's closing notes
    should point to ``ssh -L`` for desktop-browser access (rather
    than encouraging users to bind 0.0.0.0 without TLS)."""
    assert "ssh -L" in script_text
