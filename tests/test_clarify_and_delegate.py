"""tests/test_clarify_and_delegate.py — v1.8.0 Tier A item 2."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from janus import config
from janus.tools.clarify import Clarify, UNAVAILABLE, MAX_CHOICES
from janus.tools.delegate import Delegate, _THREAD_LOCAL


def _approve(*a, **kw):
    return True


def _deny(*a, **kw):
    return False


# ---------- Clarify ----------


def test_clarify_returns_unavailable_with_no_callback():
    """Bundled registration uses no callback → headless / sub-agent
    contexts get a clear sentinel instead of crashing."""
    out = Clarify().run({"question": "which?", "choices": ["a", "b"]}, _approve)
    assert out == UNAVAILABLE


def test_clarify_invokes_callback_with_question_and_choices():
    captured = {}

    def cb(question, choices):
        captured["q"] = question
        captured["c"] = choices
        return "the answer"

    out = Clarify(callback=cb).run(
        {"question": "which?", "choices": ["a", "b"]}, _approve,
    )
    assert out == "the answer"
    assert captured["q"] == "which?"
    assert captured["c"] == ["a", "b"]


def test_clarify_open_ended_question_passes_none_for_choices():
    captured = {}

    def cb(question, choices):
        captured["c"] = choices
        return "free text answer"

    Clarify(callback=cb).run({"question": "what?"}, _approve)
    assert captured["c"] is None


def test_clarify_strips_blank_choices():
    captured = {}

    def cb(question, choices):
        captured["c"] = choices
        return "x"

    Clarify(callback=cb).run(
        {"question": "?", "choices": ["a", "  ", "b", ""]}, _approve,
    )
    assert captured["c"] == ["a", "b"]


def test_clarify_caps_choices_at_max():
    too_many = [f"c{i}" for i in range(MAX_CHOICES + 5)]
    captured = {}

    def cb(question, choices):
        captured["n"] = len(choices)
        return "x"

    Clarify(callback=cb).run({"question": "?", "choices": too_many}, _approve)
    assert captured["n"] == MAX_CHOICES


def test_clarify_rejects_empty_question():
    out = Clarify(callback=lambda q, c: "x").run({"question": "  "}, _approve)
    assert out.startswith("error:")


def test_clarify_truncates_long_question():
    captured = {}

    def cb(q, c):
        captured["q"] = q
        return "x"

    Clarify(callback=cb).run({"question": "x" * 1000}, _approve)
    assert len(captured["q"]) <= 500
    assert captured["q"].endswith("…")


def test_clarify_callback_returning_none_yields_unavailable():
    out = Clarify(callback=lambda q, c: None).run({"question": "?"}, _approve)
    assert out == UNAVAILABLE


def test_clarify_callback_exception_returns_error_string():
    def cb(q, c):
        raise RuntimeError("ui crashed")

    out = Clarify(callback=cb).run({"question": "?"}, _approve)
    assert out.startswith("error:")
    assert "ui crashed" in out


def test_clarify_in_default_registry():
    from janus.tools import default_registry
    reg = default_registry()
    assert "clarify" in reg.names()


# ---------- Delegate ----------


def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "janus_home"
    home.mkdir()
    monkeypatch.setattr(config, "HOME", home)
    monkeypatch.setattr(config, "SKILLS_DIR", home / "skills")
    monkeypatch.setattr(config, "TRIGGERS_DIR", home / "triggers")
    monkeypatch.setattr(config, "MEMORY_DIR", home / "memory")
    monkeypatch.setattr(config, "USER_MODEL_FILE", home / "user.md")
    monkeypatch.setattr(config, "LOG_FILE", home / "log.jsonl")
    monkeypatch.setattr(config, "DAEMON_STATE", home / "daemon.state.json")
    monkeypatch.setattr(config, "EVALS_DIR", home / "evals")
    monkeypatch.setattr(config, "MCP_DIR", home / "mcp")
    monkeypatch.setattr(config, "CONVERSATIONS_DIR", home / "conversations")
    monkeypatch.setattr(config, "COMMANDS_DIR", home / "commands")
    monkeypatch.setattr(config, "SWARM_SPECS_DIR", home / "swarms" / "specs")
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", home / "swarms" / "runs")
    config.ensure_home()
    # Reset thread-local so previous tests' depth doesn't bleed.
    _THREAD_LOCAL.delegate_depth = 0


def test_delegate_invokes_executor_chat_with_restricted_tools(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = {}

    def _fake_chat(**kw):
        captured.update(kw)
        return ("subagent done", [])

    monkeypatch.setattr("janus.executor.chat", _fake_chat)

    out = Delegate().run({
        "task": "look up X and report",
    }, _approve)
    assert "subagent done" in out
    assert captured["mode"] == "auto"
    assert captured["messages"] == []
    assert captured["user_input"] == "look up X and report"
    assert captured["stream"] is False
    # Default tool surface is read-only safe set.
    tool_names = {t["function"]["name"] for t in captured["tools"].schemas()}
    assert "fs_read" in tool_names
    assert "fs_write" not in tool_names  # default is read-only
    assert "shell" not in tool_names


def test_delegate_honors_tool_names_override(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    captured = {}
    monkeypatch.setattr(
        "janus.executor.chat",
        lambda **kw: (captured.update(kw) or ("ok", [])),
    )
    Delegate().run({
        "task": "x",
        "tool_names": ["fs_read", "fs_write"],
    }, _approve)
    tool_names = {t["function"]["name"] for t in captured["tools"].schemas()}
    assert tool_names == {"fs_read", "fs_write"}


def test_delegate_clamps_max_steps(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    seen_max = []

    def _fake_chat(**kw):
        seen_max.append(config.MAX_STEPS)
        return ("ok", [])

    monkeypatch.setattr("janus.executor.chat", _fake_chat)

    Delegate().run({"task": "x", "max_steps": 999}, _approve)
    assert seen_max[0] == 20  # clamped down

    Delegate().run({"task": "x", "max_steps": 0}, _approve)
    assert seen_max[1] == 1  # clamped up


def test_delegate_recursion_blocked(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    _THREAD_LOCAL.delegate_depth = 1  # simulate being inside a delegate already
    out = Delegate().run({"task": "x"}, _approve)
    assert "recursion blocked" in out


def test_delegate_refusal_when_approver_denies(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = Delegate().run({"task": "x"}, _deny)
    assert out.startswith("refused:")


def test_delegate_rejects_empty_task(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    out = Delegate().run({"task": "  "}, _approve)
    assert out.startswith("error:")


def test_delegate_truncates_huge_subagent_output(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "janus.executor.chat",
        lambda **kw: ("X" * 12000, []),
    )
    out = Delegate().run({"task": "x"}, _approve)
    assert "more chars" in out
    assert len(out) < 12000


def test_delegate_in_default_registry():
    from janus.tools import default_registry
    reg = default_registry()
    assert "delegate" in reg.names()


def test_delegate_restores_max_steps_after_run(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    orig = config.MAX_STEPS
    monkeypatch.setattr("janus.executor.chat", lambda **kw: ("ok", []))
    Delegate().run({"task": "x", "max_steps": 5}, _approve)
    assert config.MAX_STEPS == orig


def test_delegate_recursion_depth_resets_on_exception(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)

    def _crash(**kw):
        raise RuntimeError("boom")

    monkeypatch.setattr("janus.executor.chat", _crash)
    out = Delegate().run({"task": "x"}, _approve)
    assert out.startswith("error:")
    # Critical: depth must reset even if the subagent crashes, otherwise
    # the next delegate call from the same thread is wrongly blocked.
    assert getattr(_THREAD_LOCAL, "delegate_depth", 0) == 0
