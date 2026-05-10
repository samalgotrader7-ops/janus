"""Tests for v1.38.4 — external_cli_base shared helpers (Phase 10.2.4).

The 4 existing wrapper tests (claude_code / aider / codex_cli /
gemini_cli) already cover end-to-end behavior. This file pins the
shared base API directly so a future change to external_cli_base
that breaks the contract surfaces here, not via 4 wrapper tests.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from janus.tools import external_cli_base as eb


# ---------- ANSI strip ----------


def test_strip_ansi_removes_csi():
    assert eb.strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert eb.strip_ansi("\x1b[1;33;40mhello\x1b[0m") == "hello"


def test_strip_ansi_no_ansi():
    assert eb.strip_ansi("plain text") == "plain text"


def test_strip_ansi_empty():
    assert eb.strip_ansi("") == ""


# ---------- truncate ----------


def test_truncate_under_limit_unchanged():
    assert eb.truncate("hi") == "hi"


def test_truncate_over_limit_appends_marker():
    big = "x" * 100_000
    out = eb.truncate(big)
    assert len(out) <= eb.MAX_OUTPUT_BYTES + 100
    assert "truncated" in out.lower()


def test_truncate_custom_limit():
    out = eb.truncate("a" * 200, limit=100)
    assert len(out) <= 100


# ---------- find_binary ----------


def test_find_binary_uses_path(monkeypatch):
    monkeypatch.setattr(eb.shutil, "which",
                        lambda x: "/usr/bin/foo" if x == "foo" else None)
    monkeypatch.delenv("FOO_BIN", raising=False)
    assert eb.find_binary("foo", "FOO_BIN") == "/usr/bin/foo"


def test_find_binary_env_override_preferred(monkeypatch):
    monkeypatch.setenv("FOO_BIN", "/custom/foo")
    monkeypatch.setattr(eb.shutil, "which",
                        lambda x: "/custom/foo" if x == "/custom/foo" else None)
    assert eb.find_binary("foo", "FOO_BIN") == "/custom/foo"


def test_find_binary_env_absolute_fallback(monkeypatch):
    """When env var holds an absolute path that shutil.which can't
    resolve (e.g. has a custom name), fall back to file check."""
    monkeypatch.setenv("FOO_BIN", "/abs/foo")
    monkeypatch.setattr(eb.shutil, "which", lambda x: None)
    monkeypatch.setattr(eb.os.path, "isabs", lambda p: True)
    monkeypatch.setattr(eb.os.path, "isfile", lambda p: p == "/abs/foo")
    assert eb.find_binary("foo", "FOO_BIN") == "/abs/foo"


def test_find_binary_not_found(monkeypatch):
    monkeypatch.delenv("FOO_BIN", raising=False)
    monkeypatch.setattr(eb.shutil, "which", lambda x: None)
    assert eb.find_binary("foo", "FOO_BIN") is None


# ---------- normalize_extra_args ----------


def test_normalize_none():
    assert eb.normalize_extra_args(None) == []


def test_normalize_empty_string():
    assert eb.normalize_extra_args("") == []


def test_normalize_single_string_shlex_split():
    assert eb.normalize_extra_args("--a 1 --b") == ["--a", "1", "--b"]


def test_normalize_quoted_string():
    assert eb.normalize_extra_args('--name "two words"') == ["--name", "two words"]


def test_normalize_list_drops_blanks():
    assert eb.normalize_extra_args(["--a", "", "  ", "--b"]) == ["--a", "--b"]


# ---------- env_flags ----------


def test_env_flags_unset(monkeypatch):
    monkeypatch.delenv("FOO_FLAGS", raising=False)
    assert eb.env_flags("FOO_FLAGS") == []


def test_env_flags_set(monkeypatch):
    monkeypatch.setenv("FOO_FLAGS", "--debug --verbose")
    assert eb.env_flags("FOO_FLAGS") == ["--debug", "--verbose"]


# ---------- clamp_timeout ----------


def test_clamp_timeout_default_when_none():
    assert eb.clamp_timeout(None, default=60, cap=600) == 60


def test_clamp_timeout_under_cap():
    assert eb.clamp_timeout(120, default=60, cap=600) == 120


def test_clamp_timeout_over_cap():
    assert eb.clamp_timeout(99999, default=60, cap=600) == 600


def test_clamp_timeout_minimum_one():
    assert eb.clamp_timeout(0, default=60, cap=600) == 1
    assert eb.clamp_timeout(-5, default=60, cap=600) == 1


def test_clamp_timeout_garbage_returns_default():
    assert eb.clamp_timeout("not-a-number", default=60, cap=600) == 60


# ---------- execute ----------


def test_execute_happy_path(monkeypatch, tmp_path):
    fake = MagicMock(returncode=0, stdout="ok\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=30, name="foo", binary_path="/fake/bin",
    )
    assert out.strip() == "ok"


def test_execute_zero_exit_no_stdout(monkeypatch, tmp_path):
    fake = MagicMock(returncode=0, stdout="   ", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=30, name="foo", binary_path="/fake/bin",
    )
    assert "completed" in out.lower()
    assert "exit 0" in out


def test_execute_nonzero_exit_stderr_first(monkeypatch, tmp_path):
    fake = MagicMock(returncode=2, stdout="O", stderr="E")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=30, name="foo", binary_path="/fake/bin",
    )
    assert "exit 2" in out
    assert out.index("E") < out.index("O")


def test_execute_timeout(monkeypatch, tmp_path):
    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(
            cmd="bin", timeout=10, output="partial", stderr="warn",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=10, name="foo", binary_path="/fake/bin",
    )
    assert "timed out" in out.lower()
    assert "partial" in out
    assert "warn" in out


def test_execute_filenotfound(monkeypatch, tmp_path):
    def fake_run(*a, **kw):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=30, name="foo", binary_path="/fake/bin",
    )
    assert "/fake/bin" in out
    assert "verify" in out.lower()


def test_execute_oserror(monkeypatch, tmp_path):
    def fake_run(*a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=30, name="foo", binary_path="/fake/bin",
    )
    assert "spawn failed" in out.lower()
    assert "PermissionError" in out


def test_execute_strips_ansi_in_output(monkeypatch, tmp_path):
    fake = MagicMock(returncode=0, stdout="\x1b[31mred\x1b[0m", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = eb.execute(
        cmd=["/fake/bin"], cwd=str(tmp_path),
        timeout=30, name="foo", binary_path="/fake/bin",
    )
    assert "\x1b" not in out
    assert "red" in out


# ---------- request_approval ----------


def test_request_approval_passes_capability_token():
    seen = {}

    def approver(action, details, **kw):
        seen["action"] = action
        seen["capability"] = kw.get("capability")
        seen["details"] = details
        return True

    ok = eb.request_approval(
        approver=approver,
        name="foo_cli",
        prompt="do thing",
        cwd="/path",
        timeout=120,
    )
    assert ok is True
    assert seen["action"] == "run foo_cli"
    assert seen["capability"] == ("external_cli", "foo_cli", "exec")
    assert "do thing" in seen["details"]
    assert "/path" in seen["details"]
    assert "120s" in seen["details"]


def test_request_approval_extra_lines_appended():
    seen = {}

    def approver(action, details, **kw):
        seen["details"] = details
        return False

    eb.request_approval(
        approver=approver,
        name="foo",
        prompt="x",
        cwd="/p",
        timeout=10,
        extra_lines="   files: a.py, b.py\n",
    )
    assert "files: a.py, b.py" in seen["details"]


def test_request_approval_long_prompt_truncated():
    seen = {}

    def approver(action, details, **kw):
        seen["details"] = details
        return False

    eb.request_approval(
        approver=approver,
        name="foo",
        prompt="x" * 500,
        cwd="/p",
        timeout=10,
    )
    # 200-char cap + ellipsis
    assert "…" in seen["details"]


# ---------- version ----------


def test_version_bumped_to_1_38_4():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 38, 4)
