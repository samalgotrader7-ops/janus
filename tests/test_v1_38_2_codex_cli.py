"""Tests for v1.38.2 — OpenAI Codex CLI wrapper (Phase 10.2.2)."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from janus.tools import codex_cli as cx, default_registry


def test_codex_in_default_registry():
    assert "codex_cli" in default_registry().names()


def test_schema():
    s = cx.CodexCli().schema()["function"]
    assert s["name"] == "codex_cli"
    assert "extra_args" in s["parameters"]["properties"]
    assert s["parameters"]["required"] == ["prompt"]


def test_dangerous_exec():
    t = cx.CodexCli()
    assert t.dangerous is True
    assert t.risk == "exec"


def test_empty_prompt_rejected():
    out = cx.CodexCli().run({"prompt": ""}, lambda *a, **kw: True)
    assert "empty prompt" in out.lower()


def test_nonexistent_cwd_rejected(tmp_path):
    out = cx.CodexCli().run(
        {"prompt": "x", "cwd": str(tmp_path / "nope")},
        lambda *a, **kw: True,
    )
    assert "cwd does not exist" in out.lower()


def test_missing_binary(monkeypatch):
    monkeypatch.setattr(cx, "_codex_binary", lambda: None)
    out = cx.CodexCli().run({"prompt": "x"}, lambda *a, **kw: True)
    assert "openai/codex" in out.lower() or "not found" in out.lower()


def test_approver_capability(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    seen = {}

    def app(action, details, **kw):
        seen["cap"] = kw.get("capability")
        return False

    cx.CodexCli().run({"prompt": "x", "cwd": str(tmp_path)}, app)
    assert seen["cap"] == ("external_cli", "codex_cli", "exec")


def test_command_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run(
        {"prompt": "do x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert cmd[0] == "/fake/codex"
    assert cmd[1] == "exec"
    # prompt is the LAST positional arg
    assert cmd[-1] == "do x"


def test_extra_args_appended(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run(
        {
            "prompt": "do x",
            "cwd": str(tmp_path),
            "extra_args": ["--json", "--model", "gpt-5"],
        },
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert "--json" in cmd
    assert "--model" in cmd
    assert "gpt-5" in cmd
    # prompt still last
    assert cmd[-1] == "do x"


def test_extra_args_string_is_shlex_split(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run(
        {
            "prompt": "x",
            "cwd": str(tmp_path),
            "extra_args": '--model "gpt-5 turbo" --json',
        },
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert "--json" in cmd
    assert "gpt-5 turbo" in cmd  # quoted spaces preserved


def test_env_flags_inserted(monkeypatch, tmp_path):
    monkeypatch.setenv("JANUS_CODEX_FLAGS", "--no-color")
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert "--no-color" in captured["cmd"]


def test_caller_extra_args_after_env_flags(monkeypatch, tmp_path):
    """Pin: when both env_flags and extra_args set --model, the
    caller's wins (later position on the command line)."""
    monkeypatch.setenv("JANUS_CODEX_FLAGS", "--model gpt-4")
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run(
        {
            "prompt": "x",
            "cwd": str(tmp_path),
            "extra_args": ["--model", "gpt-5"],
        },
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    # Both --model values present (codex chooses the last); caller's
    # gpt-5 comes AFTER env's gpt-4
    gpt4_idx = cmd.index("gpt-4")
    gpt5_idx = cmd.index("gpt-5")
    assert gpt5_idx > gpt4_idx


def test_default_timeout_300(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["t"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert captured["t"] == 300


def test_timeout_capped(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "TIMEOUT_MAX", 600)
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    captured = {}

    def fake_run(cmd, **kw):
        captured["t"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cx.CodexCli().run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 99999},
        lambda *a, **kw: True,
    )
    assert captured["t"] == 600


def test_timeout_partial_output(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="codex", timeout=5,
                                        output="partial", stderr="warn")

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = cx.CodexCli().run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 5},
        lambda *a, **kw: True,
    )
    assert "timed out" in out.lower()
    assert "partial" in out


def test_nonzero_exit_stderr_first(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    fake = MagicMock(returncode=1, stdout="O", stderr="E")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = cx.CodexCli().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert out.index("E") < out.index("O")


def test_ansi_stripped(monkeypatch, tmp_path):
    monkeypatch.setattr(cx, "_codex_binary", lambda: "/fake/codex")
    fake = MagicMock(returncode=0, stdout="\x1b[33myellow\x1b[0m", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = cx.CodexCli().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "\x1b" not in out
    assert "yellow" in out


def test_normalize_extra_args_helpers():
    assert cx._normalize_extra_args(None) == []
    assert cx._normalize_extra_args("") == []
    assert cx._normalize_extra_args("--json") == ["--json"]
    assert cx._normalize_extra_args("--a 1 --b 2") == ["--a", "1", "--b", "2"]
    assert cx._normalize_extra_args(["--a", "", "--b"]) == ["--a", "--b"]


def test_janus_codex_bin_override(monkeypatch):
    monkeypatch.setenv("JANUS_CODEX_BIN", "/custom/codex")
    monkeypatch.setattr(cx.shutil, "which", lambda x: None)
    monkeypatch.setattr(cx.os.path, "isfile", lambda p: p == "/custom/codex")
    monkeypatch.setattr(cx.os.path, "isabs", lambda p: True)
    assert cx._codex_binary() == "/custom/codex"


def test_version_bumped_to_1_38_2():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 38, 2)
