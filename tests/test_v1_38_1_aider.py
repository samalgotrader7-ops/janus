"""Tests for v1.38.1 — Aider CLI wrapper (Phase 10.2.1).

Coverage parallels v1.38.0 (claude_code) — same shape, different
flags. Adds files-arg specific tests.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from janus.tools import aider as ai, default_registry


# ---------- registry ----------


def test_aider_in_default_registry():
    reg = default_registry()
    assert "aider" in reg.names()


def test_aider_schema_has_required_fields():
    tool = ai.Aider()
    schema = tool.schema()
    fn = schema["function"]
    assert fn["name"] == "aider"
    assert "prompt" in fn["parameters"]["properties"]
    assert "files" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["prompt"]


def test_aider_is_dangerous_exec():
    tool = ai.Aider()
    assert tool.dangerous is True
    assert tool.risk == "exec"


# ---------- early validation ----------


def test_empty_prompt_rejected():
    out = ai.Aider().run({"prompt": ""}, lambda *a, **kw: True)
    assert "empty prompt" in out.lower()


def test_nonexistent_cwd_rejected(tmp_path):
    out = ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path / "no-such")},
        lambda *a, **kw: True,
    )
    assert "cwd does not exist" in out.lower()


def test_missing_binary_returns_install_hint(monkeypatch):
    monkeypatch.setattr(ai, "_aider_binary", lambda: None)
    out = ai.Aider().run({"prompt": "x"}, lambda *a, **kw: True)
    assert "aider-chat" in out.lower() or "not found" in out.lower()


# ---------- approver flow ----------


def test_approver_refusal_short_circuits(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    out = ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: False,
    )
    assert "refused" in out.lower()


def test_approver_receives_capability_token(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    seen = {}

    def approver(action, details, **kw):
        seen["capability"] = kw.get("capability")
        return False

    ai.Aider().run({"prompt": "x", "cwd": str(tmp_path)}, approver)
    assert seen["capability"] == ("external_cli", "aider", "exec")


# ---------- happy path ----------


def test_happy_path_returns_stdout(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    fake = MagicMock(returncode=0, stdout="committed: refactor\n", stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)

    out = ai.Aider().run(
        {"prompt": "refactor X", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "committed: refactor" in out


def test_command_includes_message_and_yes_always(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai.Aider().run(
        {"prompt": "do x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert "/fake/aider" in cmd[0]
    assert "--message" in cmd
    msg_idx = cmd.index("--message")
    assert cmd[msg_idx + 1] == "do x"
    assert "--yes-always" in cmd
    assert "--no-stream" in cmd


def test_files_arg_appends_file_flags(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai.Aider().run(
        {
            "prompt": "x",
            "cwd": str(tmp_path),
            "files": ["src/foo.py", "src/bar.py"],
        },
        lambda *a, **kw: True,
    )
    cmd = captured["cmd"]
    assert cmd.count("--file") == 2
    # Both file paths should appear after their respective --file flags
    assert "src/foo.py" in cmd
    assert "src/bar.py" in cmd


def test_files_arg_accepts_string_as_single_path(monkeypatch, tmp_path):
    """Pin: if model passes a string instead of a list, treat as one path."""
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    captured = {}

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path), "files": "src/foo.py"},
        lambda *a, **kw: True,
    )
    assert "src/foo.py" in captured["cmd"]
    assert captured["cmd"].count("--file") == 1


def test_default_timeout_is_300(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    captured = {}

    def fake_run(cmd, **kw):
        captured["timeout"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai.Aider().run({"prompt": "x", "cwd": str(tmp_path)}, lambda *a, **kw: True)
    assert captured["timeout"] == 300


def test_timeout_capped_at_max(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "TIMEOUT_MAX", 600)
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    captured = {}

    def fake_run(cmd, **kw):
        captured["timeout"] = kw.get("timeout")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 99999},
        lambda *a, **kw: True,
    )
    assert captured["timeout"] == 600


# ---------- failure modes ----------


def test_timeout_returns_partial_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")

    def fake_run(*a, **kw):
        raise subprocess.TimeoutExpired(
            cmd="aider", timeout=10,
            output="working...", stderr="slow",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    out = ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path), "timeout": 10},
        lambda *a, **kw: True,
    )
    assert "timed out" in out.lower()
    assert "working..." in out
    assert "slow" in out


def test_nonzero_exit_returns_stderr_first(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    fake = MagicMock(returncode=1, stdout="some output", stderr="not in a git repo")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)

    out = ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "exit 1" in out
    assert "not in a git repo" in out
    assert out.index("not in a git repo") < out.index("some output")


# ---------- output cleanup ----------


def test_ansi_stripped(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    fake = MagicMock(
        returncode=0,
        stdout="\x1b[32mgreen\x1b[0m text",
        stderr="",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert "\x1b" not in out
    assert "green" in out


def test_output_truncated_at_max_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(ai, "_aider_binary", lambda: "/fake/aider")
    fake = MagicMock(returncode=0, stdout="x" * 100_000, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake)
    out = ai.Aider().run(
        {"prompt": "x", "cwd": str(tmp_path)},
        lambda *a, **kw: True,
    )
    assert len(out) <= ai.MAX_OUTPUT_BYTES + 100
    assert "truncated" in out.lower()


# ---------- normalize files ----------


def test_normalize_files_handles_none():
    assert ai._normalize_files(None) == []


def test_normalize_files_handles_empty_string():
    assert ai._normalize_files("") == []


def test_normalize_files_handles_string():
    assert ai._normalize_files("foo.py") == ["foo.py"]


def test_normalize_files_handles_list_with_blanks():
    assert ai._normalize_files(["foo.py", "", "  ", "bar.py"]) == [
        "foo.py", "bar.py",
    ]


# ---------- env override ----------


def test_janus_aider_bin_env_override(monkeypatch):
    monkeypatch.setenv("JANUS_AIDER_BIN", "/custom/aider")
    monkeypatch.setattr(ai.shutil, "which", lambda x: None)
    monkeypatch.setattr(ai.os.path, "isfile", lambda p: p == "/custom/aider")
    monkeypatch.setattr(ai.os.path, "isabs", lambda p: True)
    binary = ai._aider_binary()
    assert binary == "/custom/aider"


# ---------- version ----------


def test_version_bumped_to_1_38_1():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 38, 1)
