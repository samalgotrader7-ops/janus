"""
services.py — install / manage Janus background services (v1.16.0).

WHY THIS EXISTS:
Sam's pain: 3 tmux windows for telegram + cli + daemon. The CLI is
interactive (you talk to it), but telegram and daemon are daemons —
they should run as system services, not as user-attended terminals.

Hermes ships systemd integration in setup-hermes.sh. Janus didn't,
so users like Sam manually started gateways in tmux and crossed
fingers that nothing crashed mid-session.

v1.16 fixes this with a `janus service` CLI subcommand that:

  - generates user-mode systemd unit files (~/.config/systemd/user/)
  - installs them
  - starts + enables them (so they survive reboot)
  - shows status / logs
  - uninstalls cleanly

USER MODE (systemctl --user) NOT SYSTEM:
- no sudo needed
- units run as the invoking user with full access to ~/.janus/
- on logout the user-systemd is gone unless `loginctl enable-linger`
  is set (we tell the user to run that for headless servers)

WHY systemd ONLY (no launchd / open-rc / runit / sysvinit / plain bg):
- it's the dominant init on the linux servers Sam targets
- adding launchd / sysvinit doubles the maintenance surface for ~2%
  more users
- on systemd-less platforms (Termux, Alpine, macOS, NixOS) we print
  the equivalent shell commands the user can run with whatever init
  they prefer (good enough for now; build out if asked)

UNIT FILES:
~/.config/systemd/user/janus-telegram.service
  ExecStart=/path/to/janus telegram
  Restart=always (5s backoff)
  EnvironmentFile=~/.janus/.env
  Logs to journalctl --user -u janus-telegram

~/.config/systemd/user/janus-daemon.service
  ExecStart=/path/to/janus daemon
  Same restart + env policy
  Logs to journalctl --user -u janus-daemon

P5 (plain-text state): the unit files are .ini files the user can
read + edit. We never hide what's running; install just generates
the right files.
"""

from __future__ import annotations
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from . import config


# Service definitions: (name, exec_args, description)
SERVICES = [
    {
        "name": "janus-telegram",
        "exec_args": ["telegram"],
        "description": "Janus Telegram gateway",
        "needs_env": ["JANUS_TELEGRAM_TOKEN"],
    },
    {
        "name": "janus-daemon",
        "exec_args": ["daemon"],
        "description": "Janus trigger daemon (scheduled agents)",
        "needs_env": [],
    },
]


# ---------- systemd detection ----------


def have_systemd() -> bool:
    """True iff systemctl is on PATH AND user-mode is available.

    On a fresh Ubuntu server systemctl exists but `--user` may not
    work without an active D-Bus user session. We probe with
    `systemctl --user is-system-running` (returns running/degraded/
    starting/etc.) — non-zero exit means user-systemd isn't usable.
    """
    if not shutil.which("systemctl"):
        return False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True, text=True, timeout=5,
        )
        # Status text varies (running / degraded / starting / offline);
        # anything except 'offline' / non-zero exit indicates a usable
        # user manager.
        if r.returncode != 0:
            return False
        out = (r.stdout or "").strip().lower()
        return out not in ("offline", "")
    except (subprocess.TimeoutExpired, OSError):
        return False


def user_unit_dir() -> Path:
    """The standard user systemd unit directory."""
    return Path.home() / ".config" / "systemd" / "user"


def janus_binary_path() -> str:
    """Where the 'janus' executable lives on PATH.

    pipx install puts it at ~/.local/bin/janus (or %USERPROFILE%\\.local\\bin
    on Windows). We prefer shutil.which('janus') because that's what
    the user's shell would actually pick. Fall back to sys.executable
    + -m janus when the binary isn't on PATH."""
    found = shutil.which("janus")
    if found:
        return found
    return f"{sys.executable} -m janus"


# ---------- Unit-file rendering ----------


def render_unit(service: dict) -> str:
    """Render a single .service file as a string."""
    binary = janus_binary_path()
    if " " in binary and not binary.startswith('"'):
        # `python -m janus` form — leave unquoted but handle args properly
        exec_start = f"{binary} {' '.join(service['exec_args'])}"
    else:
        exec_start = f"{binary} {' '.join(service['exec_args'])}"

    env_file = config.HOME / ".env"
    workspace = str(config.WORKSPACE)

    lines = [
        "[Unit]",
        f"Description={service['description']}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={exec_start}",
        # Auto-restart on failure but rate-limit so we don't spin on
        # a config error.
        "Restart=on-failure",
        "RestartSec=5s",
        "StartLimitIntervalSec=60s",
        "StartLimitBurst=5",
        # Working directory + env
        f"WorkingDirectory={workspace}",
        # EnvironmentFile is OPTIONAL via the leading `-` so the unit
        # starts even if .env doesn't exist yet.
        f"EnvironmentFile=-{env_file}",
        # Standard-out + err go to journal — readable via `journalctl
        # --user -u <name>`.
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=default.target",
    ]
    return "\n".join(lines) + "\n"


def install_unit(service: dict, *, force: bool = False) -> tuple[bool, str]:
    """Write the unit file. Returns (installed, message)."""
    target = user_unit_dir() / f"{service['name']}.service"
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not force:
        existing = target.read_text(encoding="utf-8")
        new = render_unit(service)
        if existing == new:
            return False, f"{service['name']}: already installed (no change)"
        return False, (
            f"{service['name']}: already installed but DIFFERENT — "
            f"re-run with --force to overwrite"
        )
    target.write_text(render_unit(service), encoding="utf-8")
    return True, f"{service['name']}: wrote {target}"


def remove_unit(service: dict) -> tuple[bool, str]:
    target = user_unit_dir() / f"{service['name']}.service"
    if not target.exists():
        return False, f"{service['name']}: not installed"
    try:
        target.unlink()
    except OSError as e:
        return False, f"{service['name']}: failed to remove: {e}"
    return True, f"{service['name']}: removed {target}"


# ---------- systemctl actions ----------


def _systemctl(*args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Wrapper. Returns the CompletedProcess; never raises."""
    cmd = ["systemctl", "--user", *args]
    return subprocess.run(
        cmd, capture_output=capture, text=True, timeout=15, check=False,
    )


def daemon_reload() -> tuple[bool, str]:
    r = _systemctl("daemon-reload")
    if r.returncode != 0:
        return False, f"daemon-reload failed: {r.stderr.strip()}"
    return True, "daemon-reload ok"


def enable_service(name: str, *, now: bool = True) -> tuple[bool, str]:
    args = ["enable"]
    if now:
        args.append("--now")
    args.append(f"{name}.service")
    r = _systemctl(*args)
    if r.returncode != 0:
        return False, f"{name}: enable failed: {r.stderr.strip()}"
    return True, f"{name}: enabled (and started)" if now else f"{name}: enabled"


def disable_service(name: str, *, now: bool = True) -> tuple[bool, str]:
    args = ["disable"]
    if now:
        args.append("--now")
    args.append(f"{name}.service")
    r = _systemctl(*args)
    if r.returncode != 0:
        return False, f"{name}: disable failed: {r.stderr.strip()}"
    return True, f"{name}: disabled (and stopped)" if now else f"{name}: disabled"


def status(name: str) -> str:
    """Returns active/inactive/failed/notinstalled."""
    target = user_unit_dir() / f"{name}.service"
    if not target.exists():
        return "notinstalled"
    r = _systemctl("is-active", f"{name}.service")
    return (r.stdout or "").strip() or "unknown"


def is_enabled(name: str) -> bool:
    r = _systemctl("is-enabled", f"{name}.service")
    return (r.stdout or "").strip() == "enabled"


# ---------- High-level commands ----------


@dataclass
class _Action:
    label: str
    ok: bool
    message: str


def cmd_install(*, force: bool = False) -> int:
    """`janus service install [--force]` — generate units + reload."""
    actions: list[_Action] = []
    if not have_systemd():
        return _print_no_systemd()
    for s in SERVICES:
        ok, msg = install_unit(s, force=force)
        actions.append(_Action(s["name"], ok, msg))
        # Warn about missing required env.
        for var in s.get("needs_env", []):
            if not os.getenv(var):
                actions.append(_Action(
                    s["name"], False,
                    f"  ⚠ {var} not set — service will fail to start. "
                    f"Set it in {config.HOME / '.env'} before enabling."
                ))
    ok, msg = daemon_reload()
    actions.append(_Action("systemd", ok, msg))
    _print_actions(actions)
    print(
        "\nNext: `janus service enable` to start them now and on reboot.\n"
        "On a headless server also run: `loginctl enable-linger $USER`\n"
        "(otherwise services stop when you log out)."
    )
    return 0


def cmd_enable() -> int:
    if not have_systemd():
        return _print_no_systemd()
    actions: list[_Action] = []
    for s in SERVICES:
        ok, msg = enable_service(s["name"], now=True)
        actions.append(_Action(s["name"], ok, msg))
    _print_actions(actions)
    print(
        "\nLogs: `journalctl --user -u janus-telegram -f` "
        "(or janus-daemon)"
    )
    return 0


def cmd_disable() -> int:
    if not have_systemd():
        return _print_no_systemd()
    actions: list[_Action] = []
    for s in SERVICES:
        ok, msg = disable_service(s["name"], now=True)
        actions.append(_Action(s["name"], ok, msg))
    _print_actions(actions)
    return 0


def cmd_status() -> int:
    if not have_systemd():
        return _print_no_systemd()
    rows = []
    for s in SERVICES:
        st = status(s["name"])
        en = "yes" if is_enabled(s["name"]) else "no"
        rows.append((s["name"], st, en))
    width = max(len(r[0]) for r in rows) + 2
    print(f"{'service'.ljust(width)}  {'status':<10}  enabled")
    print(f"{'-' * width}  {'-' * 10}  -------")
    for name, st, en in rows:
        print(f"{name.ljust(width)}  {st:<10}  {en}")
    return 0


def cmd_uninstall() -> int:
    if not have_systemd():
        return _print_no_systemd()
    actions: list[_Action] = []
    for s in SERVICES:
        ok1, msg1 = disable_service(s["name"], now=True)
        actions.append(_Action(s["name"], ok1, msg1))
        ok2, msg2 = remove_unit(s)
        actions.append(_Action(s["name"], ok2, msg2))
    ok, msg = daemon_reload()
    actions.append(_Action("systemd", ok, msg))
    _print_actions(actions)
    return 0


def cmd_show(name: str) -> int:
    """Print the rendered unit so the user can inspect what we'd install."""
    for s in SERVICES:
        if s["name"] == name or s["name"] == f"janus-{name}":
            print(render_unit(s))
            return 0
    print(f"unknown service {name!r}. Known: " +
          ", ".join(s["name"] for s in SERVICES))
    return 2


# ---------- Pretty-print helpers ----------


def _print_actions(actions: list[_Action]) -> None:
    for a in actions:
        glyph = "✓" if a.ok else "·"
        print(f"  {glyph} {a.message}")


def _print_no_systemd() -> int:
    print(
        "systemctl --user is not available on this system.\n\n"
        "On systemd Linux servers (Ubuntu/Debian/Fedora/etc.) you can run\n"
        "  janus service install\n"
        "to generate user-unit files.\n\n"
        "On other init systems (Termux, macOS, Alpine), run:\n"
        f"  {janus_binary_path()} telegram &  # in a tmux/screen session\n"
        f"  {janus_binary_path()} daemon &    # same\n\n"
        "Or write your own launchd/openrc/runit definition pointing at\n"
        f"  {janus_binary_path()} telegram\n"
        f"  {janus_binary_path()} daemon\n"
    )
    return 1
