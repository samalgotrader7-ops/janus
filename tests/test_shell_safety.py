"""Tests for shell.py v1.1.1 safety fixes — timeout cap + recursive
`janus` refusal. Both prevent the v1.1 incident where the model passed
timeout=600000 (166 hours) and the parent agent hung on subprocess.run
waiting for `janus telegram` (a daemon) to exit."""
from __future__ import annotations

import pytest

from janus import config
from janus.tools.shell import Shell, _check_recursive_janus


def _yes(*a, **kw):
    return True


# ---------- _check_recursive_janus ----------


@pytest.mark.parametrize("cmd", [
    "janus",
    "janus telegram",
    "janus web --port 8765",
    "janus -p 'do x'",
    "python -m janus",
    "python3 -m janus telegram",
    "./.venv/bin/python -m janus telegram",  # exact case from v1.1 incident
    "/usr/bin/python3.12 -m janus daemon",
    "cd /tmp && janus telegram",
    "ls; janus -p hi",
])
def test_recursive_janus_invocations_are_refused(cmd):
    refusal = _check_recursive_janus(cmd)
    assert refusal is not None
    assert "recursive" in refusal


@pytest.mark.parametrize("cmd", [
    "janus --version",
    "janus -V",
    "janus --help",
    "janus --logo",
    "janus --analyze",
    "janus -a",
    "janus --conversations",
    "python -m janus --version",
    "python3 -m janus --help",
])
def test_safe_janus_subcommands_pass(cmd):
    assert _check_recursive_janus(cmd) is None


@pytest.mark.parametrize("cmd", [
    "ls",
    "git status",
    "python myscript.py",
    "python -c 'print(1)'",
    "echo janus",          # janus mentioned but not invoked
    "cat janus.py",        # filename containing janus, not the binary
    "grep janus README.md",
    "",
])
def test_unrelated_commands_pass(cmd):
    assert _check_recursive_janus(cmd) is None


# ---------- Tool integration ----------


def test_shell_run_refuses_recursive_invocation_without_approval(janus_home):
    """The refusal should fire BEFORE the approver is called, so the user
    never sees a y/N prompt for something that would deadlock anyway."""
    approver_called = []

    def watch_approver(*a, **kw):
        approver_called.append((a, kw))
        return True

    out = Shell().run({"command": "janus telegram"}, watch_approver)
    assert "refused" in out
    assert "recursive" in out
    assert approver_called == []  # never reached the approver


def test_shell_clamps_huge_timeout_to_max(janus_home, monkeypatch):
    """The v1.1 incident: model passed timeout=600000. Without a cap,
    subprocess.run blocked 166 hours. We clamp to SHELL_TIMEOUT_MAX."""
    captured = {}

    def fake_subprocess_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        # Return a successful "completed process".
        class P:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return P()

    import janus.tools.shell as sh
    monkeypatch.setattr(sh.subprocess, "run", fake_subprocess_run)

    out = Shell().run(
        {"command": "echo hi", "timeout": 600000},
        _yes,
    )
    assert captured["timeout"] == config.SHELL_TIMEOUT_MAX
    assert "clamped" in out


def test_shell_does_not_clamp_reasonable_timeout(janus_home, monkeypatch):
    captured = {}

    def fake_subprocess_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    import janus.tools.shell as sh
    monkeypatch.setattr(sh.subprocess, "run", fake_subprocess_run)

    out = Shell().run({"command": "echo hi", "timeout": 30}, _yes)
    assert captured["timeout"] == 30
    assert "clamped" not in out


def test_shell_default_timeout_used_when_omitted(janus_home, monkeypatch):
    captured = {}

    def fake_subprocess_run(*args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        class P:
            returncode = 0
            stdout = ""
            stderr = ""
        return P()

    import janus.tools.shell as sh
    monkeypatch.setattr(sh.subprocess, "run", fake_subprocess_run)

    Shell().run({"command": "echo hi"}, _yes)
    from janus.tools.shell import DEFAULT_TIMEOUT
    assert captured["timeout"] == DEFAULT_TIMEOUT
