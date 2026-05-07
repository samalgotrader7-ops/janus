"""Tests for v1.24.4: self-kill protection + live status line.

Two issues Sam reported on the live deploy after v1.24.3:

1. The agent ran `kill 44760` to "restart janus-web" — but 44760 was
   Janus's own PID. The CLI got SIGTERM mid-task ("Terminated"). Need
   the shell tool to refuse commands that target Janus's own PID,
   any ancestor PID, or its process name (killall janus / pkill python).

2. CLI gave no progress feedback during long model calls — Sam wrote
   "most of the time I do not know the Janus on CLI is working or not".
   New status_line.StatusLine emits a Claude-Code-style spinner with
   elapsed time + token count + verb description.
"""
from __future__ import annotations

import os
import time

import pytest


# ---------- self-kill detection ----------


def test_extract_kill_target_pids_basic():
    from janus.tools.shell import _extract_kill_target_pids
    assert _extract_kill_target_pids("kill 1234") == [(1234, "1234")]
    # Multiple PIDs in one command.
    pids = _extract_kill_target_pids("kill 1234 5678")
    assert (1234, "1234") in pids and (5678, "5678") in pids
    # Signal flags ignored.
    assert _extract_kill_target_pids("kill -9 999") == [(999, "999")]


def test_extract_kill_target_pids_chain_boundary():
    """PIDs after a `;` belong to the next command, not kill."""
    from janus.tools.shell import _extract_kill_target_pids
    out = _extract_kill_target_pids("kill 1234 ; echo 5678")
    pids = [p for p, _ in out]
    assert 1234 in pids
    assert 5678 not in pids


def test_extract_kill_target_pids_no_kill():
    from janus.tools.shell import _extract_kill_target_pids
    assert _extract_kill_target_pids("echo hello 1234") == []


def test_ancestor_pids_includes_self():
    from janus.tools.shell import _ancestor_pids
    pids = _ancestor_pids()
    assert os.getpid() in pids


def test_check_self_kill_refuses_own_pid():
    from janus.tools.shell import _check_self_kill
    own = os.getpid()
    out = _check_self_kill(f"kill {own}")
    assert out is not None
    assert "Janus itself" in out


def test_check_self_kill_refuses_dash_9():
    from janus.tools.shell import _check_self_kill
    own = os.getpid()
    out = _check_self_kill(f"kill -9 {own}")
    assert out is not None


def test_check_self_kill_refuses_killall_janus():
    from janus.tools.shell import _check_self_kill
    out = _check_self_kill("killall janus")
    assert out is not None
    assert "kill Janus" in out or "janus" in out.lower()


def test_check_self_kill_refuses_killall_python():
    from janus.tools.shell import _check_self_kill
    out = _check_self_kill("killall python")
    assert out is not None


def test_check_self_kill_refuses_pkill_python():
    from janus.tools.shell import _check_self_kill
    out = _check_self_kill("pkill python")
    assert out is not None


def test_check_self_kill_refuses_pkill_janus_substring():
    """`pkill janus-web` substring-matches Janus too."""
    from janus.tools.shell import _check_self_kill
    out = _check_self_kill("pkill janus-web")
    assert out is not None


def test_check_self_kill_passes_unrelated_pid():
    from janus.tools.shell import _check_self_kill
    # A PID not in the agent's ancestry.
    assert _check_self_kill("kill 999999999") is None


def test_check_self_kill_passes_unrelated_killall():
    from janus.tools.shell import _check_self_kill
    assert _check_self_kill("killall sshd") is None


def test_check_self_kill_passes_no_kill_at_all():
    from janus.tools.shell import _check_self_kill
    assert _check_self_kill("echo hi") is None
    assert _check_self_kill("ls -la") is None
    assert _check_self_kill("") is None


def test_shell_run_returns_refusal_on_self_kill():
    """The Shell.run() entry point returns the refusal as the tool
    output, never executes the command."""
    from janus.tools.shell import Shell
    own = os.getpid()
    tool = Shell()
    out = tool.run({"command": f"kill {own}"}, lambda *a, **kw: True)
    assert "Janus itself" in out


# ---------- status line ----------


def test_fmt_time_seconds():
    from janus.status_line import _fmt_time
    assert _fmt_time(5) == "5s"
    assert _fmt_time(45) == "45s"


def test_fmt_time_minutes():
    from janus.status_line import _fmt_time
    assert _fmt_time(67) == "1m 7s"
    assert _fmt_time(720) == "12m 0s"


def test_fmt_time_hours():
    from janus.status_line import _fmt_time
    assert _fmt_time(3700) == "1h 1m"


def test_fmt_tokens():
    from janus.status_line import _fmt_tokens
    assert _fmt_tokens(0) == "0"
    assert _fmt_tokens(500) == "500"
    assert _fmt_tokens(1500) == "1.5k"
    assert _fmt_tokens(12_400) == "12.4k"
    assert _fmt_tokens(1_500_000) == "1.5M"


def test_verb_for_tool_known():
    from janus.status_line import verb_for_tool
    assert verb_for_tool("shell") == "running shell"
    assert verb_for_tool("fs_read") == "reading file"
    assert verb_for_tool("memory_search") == "searching memory"
    assert verb_for_tool("web_fetch") == "fetching url"


def test_verb_for_tool_unknown():
    from janus.status_line import verb_for_tool
    assert verb_for_tool("custom_skill_42") == "running custom_skill_42"


def test_status_line_disabled_when_no_status_line_env(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "1")
    from janus.status_line import StatusLine
    line = StatusLine()
    assert line._disabled is True


def test_status_line_enabled_when_explicit_zero(monkeypatch):
    """JANUS_NO_STATUS_LINE=0 + a real fd → enabled."""
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    # Use a StringIO-like sink. _is_status_disabled checks isatty;
    # StringIO doesn't have isatty=True. So with explicit "0" we
    # bypass the auto-detect.
    line = StatusLine(file=io.StringIO())
    assert line._disabled is False


def test_status_line_set_verb_thread_safe(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    line = StatusLine(file=io.StringIO())
    # set_verb / add_tokens / set_tokens are pure state mutations.
    line.set_verb("thinking")
    assert line.verb == "thinking"
    line.add_tokens(100)
    assert line.tokens == 100
    line.add_tokens(50)
    assert line.tokens == 150
    line.set_tokens(999)
    assert line.tokens == 999


def test_status_line_first_action_records_thought_time(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    line = StatusLine(file=io.StringIO())
    line.t0 = time.monotonic() - 5.0  # pretend we've been thinking 5s
    assert line.thought_time == 0.0
    line.set_verb("running shell")  # first non-thinking verb
    assert line.thought_time >= 4.0  # ~5s elapsed from manual t0


def test_status_line_subsequent_verbs_dont_overwrite_thought_time(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    line = StatusLine(file=io.StringIO())
    line.t0 = time.monotonic() - 5.0
    line.set_verb("running shell")
    snapshot = line.thought_time
    time.sleep(0.05)
    line.set_verb("reading file")
    # thought_time stays at the moment of the FIRST non-thinking verb.
    assert line.thought_time == snapshot


def test_status_line_format_includes_elapsed_and_verb(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    line = StatusLine(file=io.StringIO())
    line.set_verb("calling model")
    out = line._format()
    assert "calling model" in out
    # Should show elapsed (0s if just started).
    assert "0s" in out or "1s" in out


def test_status_line_format_with_tokens(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    line = StatusLine(file=io.StringIO())
    line.set_verb("thinking")
    line.set_tokens(2500)
    out = line._format()
    assert "2.5k" in out
    assert "↓" in out


def test_status_line_pause_resume_streaming(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "0")
    import io
    from janus.status_line import StatusLine
    line = StatusLine(file=io.StringIO())
    assert not line._streaming.is_set()
    line.begin_streaming()
    assert line._streaming.is_set()
    line.end_streaming()
    assert not line._streaming.is_set()


def test_status_line_clear_idempotent_when_disabled(monkeypatch):
    monkeypatch.setenv("JANUS_NO_STATUS_LINE", "1")
    from janus.status_line import StatusLine
    line = StatusLine()
    # No-op when disabled — must not raise.
    line.clear()
    line.clear()
    line.start()
    line.stop()


def test_cli_rich_step_renderer_does_not_crash_without_status(janus_home):
    """Source-level pin: render_step handles the case where
    _status_line isn't in state (e.g. cli.py basic mode)."""
    pytest.importorskip("rich")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._render_step_factory)
    # Guard logic should reference state.get("_status_line") with None
    # default — never indexes [.] which would raise KeyError.
    assert 'state.get("_status_line")' in src
