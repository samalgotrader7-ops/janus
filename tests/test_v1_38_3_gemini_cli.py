"""Tests for v1.38.3 — Google Gemini CLI wrapper (Phase 10.2.3)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from janus.tools import gemini_cli as gc, default_registry


def test_gemini_in_default_registry():
    assert "gemini_cli" in default_registry().names()


def test_schema():
    s = gc.GeminiCli().schema()["function"]
    assert s["name"] == "gemini_cli"
    assert "extra_args" in s["parameters"]["properties"]


def test_dangerous_exec():
    t = gc.GeminiCli()
    assert t.dangerous is True
    assert t.risk == "exec"


def test_empty_prompt_rejected():
    out = gc.GeminiCli().run({"prompt": ""}, lambda *a, **kw: True)
    assert "empty prompt" in out.lower()


def test_nonexistent_cwd_rejected(tmp_path):
    out = gc.GeminiCli().run(
        {"prompt": "x", "cwd": str(tmp_path / "nope")},
        lambda *a, **kw: True,
    )
    assert "cwd does not exist" in out.lower()


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: None)
    out = gc.GeminiCli().run({"prompt": "x"}, lambda *a, **kw: True)
    assert "@google/gemini-cli" in out.lower() or "not found" in out.lower()


def test_approver_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    seen = {}

    def app(action, details, **kw):
        seen["cap"] = kw.get("capability")
        return False

    gc.GeminiCli().run({"prompt": "x", "cwd": str(tmp_path)}, app)
    assert seen["cap"] == ("external_cli", "gemini_cli", "exec")


def test_command_shape_uses_dash_p(monkeypatch, tmp_path):
    """Pin: gemini's -p flag is the print-mode trigger, last arg is prompt."""
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gc.GeminiCli().run(
        {"prompt": "explain X", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/gemini"
    assert cmd[1] == "-p"
    assert cmd[-1] == "explain X"


def test_extra_args_appended(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gc.GeminiCli().run(
        {
            "prompt": "x",
            "cwd": str(tmp_path),
            "extra_args": ["--all-files", "--model", "gemini-2.5-pro"],
        },
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert "--all-files" in cmd
    assert "gemini-2.5-pro" in cmd


def test_env_flags_inserted(monkeypatch, tmp_path):
    monkeypatch.setenv("JANUS_GEMINI_FLAGS", "--all-files --debug")
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gc.GeminiCli().run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert "--all-files" in captured["cmd"]
    assert "--debug" in captured["cmd"]


def test_default_timeout_300(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    captured = {}

    def fake_run(cmd, **kw):
        captured["t"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gc.GeminiCli().run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert captured["t"] == 300


def test_timeout_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "TIMEOUT_MAX", 600)
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    captured = {}

    def fake_run(cmd, **kw):
        captured["t"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    gc.GeminiCli().run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 99999},
        lambda *a, **kw: True,
    )
    assert captured["t"] == 600


def test_timeout_partial(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="gemini", timeout=5,
                                        output="thinking", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = gc.GeminiCli().run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 5},
        lambda *a, **kw: True,
    )
    assert "timed out" in out.lower()
    assert "thinking" in out


def test_nonzero_exit_stderr_first(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    fake = MagicMock(returncode=1, stdout="O", stderr="E")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = gc.GeminiCli().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert out.index("E") < out.index("O")


def test_ansi_stripped(monkeypatch, tmp_path):
    monkeypatch.setattr(gc, "_gemini_binary", lambda: "/fake/gemini")
    fake = MagicMock(returncode=0, stdout="\x1b[34mblue\x1b[0m", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = gc.GeminiCli().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "\x1b" not in out
    assert "blue" in out


def test_janus_gemini_bin_override(monkeypatch):
    monkeypatch.setenv("JANUS_GEMINI_BIN", "/custom/gemini")
    monkeypatch.setattr(gc.shutil, "which", lambda x: None)
    monkeypatch.setattr(gc.os.path, "isfile", lambda p: p == "/custom/gemini")
    monkeypatch.setattr(gc.os.path, "isabs", lambda p: True)
    assert gc._gemini_binary() == "/custom/gemini"


def test_version_bumped_to_1_38_3():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 38, 3)
