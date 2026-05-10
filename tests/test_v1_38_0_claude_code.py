"""Tests for v1.38.0 — Claude Code CLI wrapper (Phase 10.2.0).

Coverage:
  * tool registered in default_registry
  * missing binary returns clear error (no crash)
  * empty prompt rejected
  * cwd validation (non-existent path → error)
  * approver refusal short-circuits
  * happy path: subprocess called with expected args; stdout returned
  * timeout → returns partial stdout/stderr with timeout marker
  * ANSI escape sequences stripped from output
  * Output truncated at MAX_OUTPUT_BYTES
  * Capability token external_cli.claude_code.exec passed to approver
  * JANUS_CLAUDE_BIN env override
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from janus.tools import claude_code as cc, default_registry


# ---------- registry ----------


def test_claude_code_in_default_registry():
    reg = default_registry()
    assert "claude_code" in reg.names()


def test_claude_code_schema_has_required_fields():
    tool = cc.ClaudeCode()
    schema = tool.schema()
    fn = schema["function"]
    assert fn["name"] == "claude_code"
    assert "prompt" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["prompt"]


def test_claude_code_is_dangerous_exec():
    tool = cc.ClaudeCode()
    assert tool.dangerous is True
    assert tool.risk == "exec"


# ---------- early validation ----------


def test_empty_prompt_rejected():
    tool = cc.ClaudeCode()
    out = tool.run({"prompt": ""}, lambda *a, **kw: True)
    assert "empty prompt" in out.lower()


def test_whitespace_only_prompt_rejected():
    tool = cc.ClaudeCode()
    out = tool.run({"prompt": "   "}, lambda *a, **kw: True)
    assert "empty prompt" in out.lower()


def test_nonexistent_cwd_rejected(tmp_path):
    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "do x", "cwd": str(tmp_path / "no-such-dir")},
        lambda *a, **kw: True,
    )
    assert "cwd does not exist" in out.lower()


def test_missing_binary_returns_clear_error(monkeypatch):
    monkeypatch.setattr(cc, "_claude_binary", lambda: None)
    tool = cc.ClaudeCode()
    out = tool.run({"prompt": "do x"}, lambda *a, **kw: True)
    assert "claude" in out.lower()
    assert "binary not found" in out.lower() or "not found" in out.lower()


# ---------- approver flow ----------


def test_approver_refusal_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "do x", "cwd": str(tmp_path)},
        lambda *a, **kw: False,
    )
    assert "refused" in out.lower()


def test_approver_receives_capability_token(monkeypatch, tmp_path):
    """Pin: the approver gets capability=('external_cli', 'claude_code',
    'exec') so skill-granted users skip the prompt."""
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    seen = {}

    def approver(action, details, **kw):
        seen["action"] = action
        seen["capability"] = kw.get("capability")
        return False  # refuse, doesn't matter

    tool = cc.ClaudeCode()
    tool.run(
        {"prompt": "x", "cwd": str(tmp_path)},
        approver,
    )
    assert seen["capability"] == ("external_cli", "claude_code", "exec")


# ---------- happy path ----------


def test_happy_path_returns_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    fake_proc = MagicMock(returncode=0, stdout="all done!\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "build it", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert out.strip() == "all done!"


def test_subprocess_called_with_expected_command(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = kw.get("cwd")
        captured["timeout"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = cc.ClaudeCode()
    tool.run(
        {
            "prompt": "edit foo.py",
            "cwd": str(tmp_path),
            "timeout": 120,
            "output_format": "json",
        },
        lambda *a, **kw: True,
    )
    assert captured["cmd"] == [
        "/fake/claude", "-p", "edit foo.py",
        "--output-format", "json",
    ]
    assert captured["cwd"] == str(tmp_path)
    assert captured["timeout"] == 120


def test_default_timeout_is_300(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    captured = {}

    def fake_run(cmd, **kw):
        captured["timeout"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = cc.ClaudeCode()
    tool.run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert captured["timeout"] == 300


def test_timeout_capped_at_max(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "TIMEOUT_MAX", 600)
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    captured = {}

    def fake_run(cmd, **kw):
        captured["timeout"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = cc.ClaudeCode()
    tool.run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 99999},
        lambda *a, **kw: True,
    )
    assert captured["timeout"] == 600  # capped


def test_default_output_format_is_text(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = cc.ClaudeCode()
    tool.run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert "--output-format" in captured["cmd"]
    idx = captured["cmd"].index("--output-format")
    assert captured["cmd"][idx + 1] == "text"


def test_invalid_output_format_falls_back_to_text(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = cc.ClaudeCode()
    tool.run(
        {"prompt": "x", "cwd": str(tmp_path), "output_format": "yaml"},
        lambda *a, **kw: True,
    )
    idx = captured["cmd"].index("--output-format")
    assert captured["cmd"][idx + 1] == "text"


# ---------- failure modes ----------


def test_timeout_returns_partial_output(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(
            cmd="claude", timeout=10,
            output="halfway there",
            stderr="warning: slow",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 10},
        lambda *a, **kw: True,
    )
    assert "timed out" in out.lower()
    assert "halfway there" in out
    assert "warning: slow" in out


def test_nonzero_exit_returns_stderr_first(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    fake_proc = MagicMock(returncode=2, stdout="some output", stderr="auth required")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "exit 2" in out
    assert "auth required" in out
    assert "some output" in out
    # Stderr appears before stdout in the message
    assert out.index("auth required") < out.index("some output")


# ---------- output cleanup ----------


def test_ansi_stripped_from_output(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    fake_proc = MagicMock(
        returncode=0,
        stdout="\x1b[31mred error\x1b[0m\n\x1b[1mbold\x1b[0m text",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "\x1b" not in out
    assert "red error" in out
    assert "bold" in out


def test_output_truncated_at_max_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    big = "x" * 100_000  # 100KB
    fake_proc = MagicMock(returncode=0, stdout=big, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert len(out) <= cc.MAX_OUTPUT_BYTES + 100
    assert "truncated" in out.lower()


def test_zero_exit_no_stdout_returns_status_message(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_claude_binary", lambda: "/fake/claude")
    fake_proc = MagicMock(returncode=0, stdout="   \n  ", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_proc)

    tool = cc.ClaudeCode()
    out = tool.run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "completed" in out.lower()
    assert "exit 0" in out


# ---------- env override ----------


def test_janus_claude_bin_env_override(monkeypatch, tmp_path):
    """Pin: JANUS_CLAUDE_BIN takes precedence over PATH lookup."""
    monkeypatch.setenv("JANUS_CLAUDE_BIN", "/custom/path/to/claude")
    # shutil.which on a fake path returns None — fall through to abs path check
    monkeypatch.setattr(cc.shutil, "which", lambda x: None)
    # Pretend the absolute path exists
    monkeypatch.setattr(cc.os.path, "isfile", lambda p: p == "/custom/path/to/claude")
    monkeypatch.setattr(cc.os.path, "isabs", lambda p: True)
    binary = cc._claude_binary()
    assert binary == "/custom/path/to/claude"


# ---------- version ----------


def test_version_bumped_to_1_38_0():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 38, 0)
