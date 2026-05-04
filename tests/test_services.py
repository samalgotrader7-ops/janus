"""tests/test_services.py — v1.16.0 systemd service installation."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from janus import config, services


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "WORKSPACE", tmp_path / "workspace")
    (tmp_path / "workspace").mkdir(exist_ok=True)
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    config.ensure_home()
    # Pretend systemd unit dir is under tmp_path so tests don't touch
    # the real ~/.config/systemd/user.
    monkeypatch.setattr(
        services, "user_unit_dir",
        lambda: tmp_path / "systemd_units",
    )


# ============================================================
# Unit file rendering
# ============================================================


def test_render_unit_includes_exec_start(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "janus_binary_path", lambda: "/usr/bin/janus")
    out = services.render_unit(services.SERVICES[0])  # janus-telegram
    assert "ExecStart=/usr/bin/janus telegram" in out


def test_render_unit_includes_restart_policy(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = services.render_unit(services.SERVICES[0])
    assert "Restart=on-failure" in out
    assert "RestartSec=5s" in out


def test_render_unit_environment_file_optional(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = services.render_unit(services.SERVICES[0])
    # Leading dash means OPTIONAL — service starts even without .env
    assert "EnvironmentFile=-" in out


def test_render_unit_logs_to_journal(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = services.render_unit(services.SERVICES[0])
    assert "StandardOutput=journal" in out
    assert "StandardError=journal" in out


def test_render_unit_workspace_is_set(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = services.render_unit(services.SERVICES[0])
    assert f"WorkingDirectory={config.WORKSPACE}" in out


def test_render_unit_uses_python_dash_m_when_no_binary(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr("shutil.which", lambda x: None)
    out = services.render_unit(services.SERVICES[0])
    # Falls back to python -m janus
    assert "python" in out and "-m janus" in out


# ============================================================
# Install / remove
# ============================================================


def test_install_unit_writes_file(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ok, msg = services.install_unit(services.SERVICES[0])
    assert ok
    target = services.user_unit_dir() / "janus-telegram.service"
    assert target.is_file()


def test_install_unit_idempotent(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "janus_binary_path", lambda: "/x/janus")
    services.install_unit(services.SERVICES[0])
    ok, msg = services.install_unit(services.SERVICES[0])
    assert not ok  # second install is a no-op
    assert "already installed" in msg


def test_install_unit_force_overwrites(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "janus_binary_path", lambda: "/old/janus")
    services.install_unit(services.SERVICES[0])
    monkeypatch.setattr(services, "janus_binary_path", lambda: "/new/janus")
    ok, msg = services.install_unit(services.SERVICES[0], force=True)
    assert ok
    text = (services.user_unit_dir() / "janus-telegram.service").read_text()
    assert "/new/janus" in text


def test_install_unit_warns_on_drift(tmp_path, monkeypatch):
    """Without --force, an existing-but-different unit triggers a warning."""
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "janus_binary_path", lambda: "/v1/janus")
    services.install_unit(services.SERVICES[0])
    monkeypatch.setattr(services, "janus_binary_path", lambda: "/v2/janus")
    ok, msg = services.install_unit(services.SERVICES[0], force=False)
    assert not ok
    assert "DIFFERENT" in msg


def test_remove_unit_drops_file(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    services.install_unit(services.SERVICES[0])
    ok, msg = services.remove_unit(services.SERVICES[0])
    assert ok
    assert not (services.user_unit_dir() / "janus-telegram.service").exists()


def test_remove_unit_not_installed(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    ok, msg = services.remove_unit(services.SERVICES[0])
    assert not ok
    assert "not installed" in msg


# ============================================================
# systemd detection
# ============================================================


def test_have_systemd_no_systemctl(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: None)
    assert services.have_systemd() is False


def test_have_systemd_user_offline(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: "/bin/systemctl")
    fake_proc = MagicMock(returncode=0, stdout="offline\n", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    assert services.have_systemd() is False


def test_have_systemd_user_running(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: "/bin/systemctl")
    fake_proc = MagicMock(returncode=0, stdout="running\n", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    assert services.have_systemd() is True


def test_have_systemd_swallows_exceptions(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda x: "/bin/systemctl")
    def boom(*a, **kw):
        import subprocess as sp
        raise sp.TimeoutExpired(cmd="systemctl", timeout=5)
    monkeypatch.setattr("subprocess.run", boom)
    assert services.have_systemd() is False


# ============================================================
# Action wrappers
# ============================================================


def test_enable_service_returns_failure_message(monkeypatch):
    fake_proc = MagicMock(returncode=1, stdout="", stderr="unit not found\n")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    ok, msg = services.enable_service("ghost")
    assert not ok
    assert "enable failed" in msg


def test_enable_service_now(monkeypatch):
    captured = {}
    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setattr("subprocess.run", fake_run)
    services.enable_service("janus-telegram", now=True)
    assert "--now" in captured["cmd"]


def test_status_returns_notinstalled_when_missing_unit(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = services.status("janus-telegram")
    assert out == "notinstalled"


def test_status_returns_active_when_systemd_says_so(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    services.install_unit(services.SERVICES[0])
    fake_proc = MagicMock(returncode=0, stdout="active\n", stderr="")
    monkeypatch.setattr("subprocess.run", lambda *a, **kw: fake_proc)
    assert services.status("janus-telegram") == "active"


# ============================================================
# Top-level command output
# ============================================================


def test_cmd_install_prints_no_systemd_message(monkeypatch, capsys):
    monkeypatch.setattr(services, "have_systemd", lambda: False)
    rc = services.cmd_install()
    assert rc == 1
    captured = capsys.readouterr()
    assert "systemctl --user is not available" in captured.out


def test_cmd_show_prints_unit_for_known_service(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    rc = services.cmd_show("janus-telegram")
    assert rc == 0
    captured = capsys.readouterr()
    assert "[Service]" in captured.out
    assert "ExecStart" in captured.out


def test_cmd_show_unknown_returns_error(capsys):
    rc = services.cmd_show("bogus")
    assert rc == 2
    captured = capsys.readouterr()
    assert "unknown service" in captured.out


def test_cmd_install_writes_files(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "have_systemd", lambda: True)
    monkeypatch.setattr(services, "daemon_reload", lambda: (True, "ok"))
    rc = services.cmd_install()
    assert rc == 0
    # Both service files exist
    d = services.user_unit_dir()
    assert (d / "janus-telegram.service").is_file()
    assert (d / "janus-daemon.service").is_file()


def test_cmd_install_warns_about_missing_telegram_token(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.delenv("JANUS_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setattr(services, "have_systemd", lambda: True)
    monkeypatch.setattr(services, "daemon_reload", lambda: (True, "ok"))
    services.cmd_install()
    captured = capsys.readouterr()
    assert "JANUS_TELEGRAM_TOKEN" in captured.out
    assert "not set" in captured.out


def test_cmd_status_table(tmp_path, monkeypatch, capsys):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "have_systemd", lambda: True)
    monkeypatch.setattr(services, "is_enabled", lambda n: False)
    rc = services.cmd_status()
    assert rc == 0
    captured = capsys.readouterr()
    assert "janus-telegram" in captured.out
    assert "janus-daemon" in captured.out
    assert "service" in captured.out  # column header
