"""tests/test_fire_uses_chat.py — v1.7.0 fire_once switched to executor.chat.

Pre-v1.7 fired agents went through legacy executor.execute (interpret-then-
execute). This meant the unattended preamble in skill body applied, but the
JANUS_CHAT_SYSTEM rules (Rule 10 about agent_create, etc.) did NOT — fired
agents and chat agents were two different codepaths with two different
behavior surfaces.

These tests pin the v1.7 contract: fire_once calls executor.chat with
mode='auto', the skill body, and an empty messages list. One codepath.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from janus import config
from janus.tools.agent import AgentCreate
from janus.triggers import runtime as runtime_mod
from janus.triggers.base import load_triggers


def _approve(*a, **kw):
    return True


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


# ---------- Source-level pin: fire_once must call executor.chat ----------


def test_fire_once_source_calls_executor_chat():
    """Architectural invariant: fire_once goes through executor.chat,
    NOT executor.execute (the legacy path with its own bugs)."""
    src = inspect.getsource(runtime_mod.fire_once)
    # Must call chat
    assert "executor.chat(" in src
    # Must NOT call legacy execute (the v1.6 bug surface)
    assert "executor.execute(" not in src


def test_fire_once_passes_mode_auto():
    """Unattended fires must run in auto mode — chat-mode 'default'
    would block writes/execs at the asker-not-available, breaking
    every cron job."""
    src = inspect.getsource(runtime_mod.fire_once)
    assert 'mode="auto"' in src or "mode='auto'" in src


# ---------- Behavior: fire_once invokes chat with the right shape ----------


def test_fire_once_invokes_chat_with_skill_body(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "newsbot", "purpose": "fetch AI news",
        "schedule": "every 4 hours", "deliver_to": "log",
    }, _approve)

    captured: dict = {}

    def _fake_chat(**kw):
        captured.update(kw)
        return ("agent done", [])

    monkeypatch.setattr(runtime_mod.executor, "chat", _fake_chat)

    triggers = load_triggers()
    out = runtime_mod.fire_once(triggers["newsbot"])
    assert out == "agent done"

    # The skill body must include the unattended preamble — this is
    # what makes the agent NOT ask the user "Please confirm…".
    assert "YOU RUN UNATTENDED" in captured["skill_body"]
    assert "fetch AI news" in captured["skill_body"]
    assert captured["mode"] == "auto"
    assert captured["messages"] == []
    assert captured["user_input"] == "fetch AI news"  # purpose used as request
    assert captured["stream"] is False  # unattended → no console to stream to


def test_fire_once_honors_tool_names_in_skill_frontmatter(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "narrow", "purpose": "do narrow work",
        "schedule": "hourly", "deliver_to": "log",
        "tool_names": ["fs_read", "web_fetch"],
    }, _approve)

    captured: dict = {}

    def _fake_chat(**kw):
        captured.update(kw)
        return ("ok", [])

    monkeypatch.setattr(runtime_mod.executor, "chat", _fake_chat)

    triggers = load_triggers()
    runtime_mod.fire_once(triggers["narrow"])

    tool_names = {t["function"]["name"] for t in captured["tools"].schemas()}
    assert tool_names == {"fs_read", "web_fetch"}


# ---------- memory-write opt-in ----------


def test_fire_only_writes_memory_when_skill_opts_in(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "no-write", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)

    monkeypatch.setattr(runtime_mod.executor, "chat",
                        lambda **kw: ("output", []))

    propose_called: list = []
    monkeypatch.setattr(
        "janus.memory.propose_diff",
        lambda *a, **kw: propose_called.append((a, kw)) or [],
    )

    triggers = load_triggers()
    runtime_mod.fire_once(triggers["no-write"])
    assert propose_called == []  # opt-out by default


def test_fire_writes_memory_when_skill_has_memory_write_true(tmp_path, monkeypatch):
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "writer", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    # Patch the skill to opt in via frontmatter.
    skill_p = config.SKILLS_DIR / "writer.md"
    text = skill_p.read_text(encoding="utf-8")
    text = text.replace("state: trusted-supervised",
                        "state: trusted-supervised\nmemory-write: true")
    skill_p.write_text(text, encoding="utf-8")

    monkeypatch.setattr(runtime_mod.executor, "chat",
                        lambda **kw: ("Sam works on Hermes parity.", []))

    fake_ops = [
        {"op": "append", "category": "project", "section": "current",
         "text": "Working on Hermes parity migration."},
    ]
    monkeypatch.setattr(
        "janus.memory.propose_diff",
        lambda req, out: fake_ops,
    )

    apply_calls: list = []
    monkeypatch.setattr(
        "janus.memory.apply",
        lambda ops, category="user": apply_calls.append(list(ops)),
    )

    triggers = load_triggers()
    runtime_mod.fire_once(triggers["writer"])

    # Apply was called with the proposed ops.
    assert apply_calls
    assert apply_calls[0][0]["section"] == "current"
    # Audit trail was written.
    audit = config.MEMORY_DIR / "_audit"
    assert audit.is_dir()
    audit_files = list(audit.glob("*.md"))
    assert len(audit_files) == 1
    assert "writer" in audit_files[0].name
    body = audit_files[0].read_text(encoding="utf-8")
    assert "trigger: writer" in body
    assert "current" in body


def test_fire_memory_write_filters_to_safe_categories(tmp_path, monkeypatch):
    """soul/user/preferences are too identity-shaped to be auto-modified
    by an unattended agent. Only project + relationships allowed."""
    _isolate_home(tmp_path, monkeypatch)
    AgentCreate().run({
        "name": "writer", "purpose": "x", "schedule": "hourly",
        "deliver_to": "log",
    }, _approve)
    skill_p = config.SKILLS_DIR / "writer.md"
    text = skill_p.read_text(encoding="utf-8")
    text = text.replace("state: trusted-supervised",
                        "state: trusted-supervised\nmemory-write: true")
    skill_p.write_text(text, encoding="utf-8")

    monkeypatch.setattr(runtime_mod.executor, "chat",
                        lambda **kw: ("output", []))

    bad_ops = [
        {"op": "append", "category": "soul", "section": "x", "text": "bad"},
        {"op": "append", "category": "user", "section": "x", "text": "bad"},
        {"op": "append", "category": "preferences", "section": "x", "text": "bad"},
        {"op": "append", "category": "project", "section": "x", "text": "ok"},
    ]
    monkeypatch.setattr("janus.memory.propose_diff", lambda req, out: bad_ops)

    apply_calls: list = []
    monkeypatch.setattr(
        "janus.memory.apply",
        lambda ops, category="user": apply_calls.append(list(ops)),
    )

    triggers = load_triggers()
    runtime_mod.fire_once(triggers["writer"])

    assert apply_calls
    applied = apply_calls[0]
    assert len(applied) == 1
    assert applied[0]["category"] == "project"
