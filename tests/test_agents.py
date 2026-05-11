"""
tests/test_agents.py — coverage for the v1.41.0 janus.agents module.

Pins:
  * Bundled 'claude' agent is discoverable.
  * AgentIdentity round-trips through to_dict/from_dict.
  * AgentMemory.set/get round-trips, notes append.
  * User-defined agent in ~/.janus/agents/ overrides a bundled one
    on name conflict.
  * dispatch() returns a useful message when the agent is missing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def test_bundled_claude_agent_discoverable():
    from janus.agents import list_agents, load_agent
    names = [a.name for a in list_agents()]
    assert "claude" in names, f"bundled 'claude' missing from {names}"

    claude = load_agent("claude")
    assert claude is not None
    assert claude.identity.style == "wrapper"
    assert claude.identity.tool_names == ["claude_code"]
    # Must declare at least one skill so the dispatcher can match.
    assert len(claude.skills) >= 1


def test_load_agent_unknown_returns_none():
    from janus.agents import load_agent
    assert load_agent("definitely-not-an-agent") is None
    assert load_agent("") is None


def test_dispatch_unknown_agent_reports_useful_message():
    from janus.agents import dispatch
    msg = dispatch("zzz-nonexistent", "hello")
    assert "not found" in msg
    assert "Known agents" in msg


def test_identity_roundtrip():
    from janus.agents import AgentIdentity
    d = {
        "name": "researcher",
        "description": "Web + memory search specialist",
        "system_prompt": "You are a research specialist.",
        "model": "anthropic/claude-sonnet-4-6",
        "tool_names": ["web_fetch", "web_search"],
        "tags": ["research"],
        "style": "chat",
        "version": "1.0",
    }
    ident = AgentIdentity.from_dict(d)
    assert ident.to_dict() == d


def test_identity_normalizes_unknown_style():
    from janus.agents import AgentIdentity
    ident = AgentIdentity.from_dict({"name": "x", "style": "bogus"})
    assert ident.style == "chat"


def test_identity_requires_name():
    from janus.agents import AgentIdentity
    with pytest.raises(ValueError):
        AgentIdentity.from_dict({"name": ""})
    with pytest.raises(ValueError):
        AgentIdentity.from_dict({})


def test_agent_memory_set_get_delete(tmp_path, monkeypatch):
    # Redirect JANUS_HOME so this test doesn't pollute Sam's real ~/.janus.
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.agents import AgentMemory
    importlib.reload(__import__("janus.agents.memory", fromlist=["_"]))
    from janus.agents.memory import AgentMemory as Fresh

    m = Fresh("unit-test-agent")
    m.set("foo", "bar")
    m.set("nested", {"a": 1, "b": [2, 3]})
    assert m.get("foo") == "bar"
    assert m.get("nested") == {"a": 1, "b": [2, 3]}
    assert m.get("missing") is None
    assert m.get("missing", "default") == "default"

    assert sorted(m.keys()) == ["foo", "nested"]

    assert m.delete("foo") is True
    assert m.delete("foo") is False
    assert m.get("foo") is None

    m.append_note("first observation")
    m.append_note("second observation")
    notes = m.read_notes()
    assert "first observation" in notes
    assert "second observation" in notes


def test_agent_memory_rejects_non_json_values(tmp_path, monkeypatch):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.agents.memory import AgentMemory as Fresh
    importlib.reload(__import__("janus.agents.memory", fromlist=["_"]))
    from janus.agents.memory import AgentMemory as Fresh

    m = Fresh("unit-test-agent")
    with pytest.raises(ValueError):
        m.set("bad", object())  # not JSON-serializable
    with pytest.raises(ValueError):
        m.set("", "value")  # empty key


def test_user_agent_overrides_bundled(tmp_path, monkeypatch):
    """User-defined agent at ~/.janus/agents/claude/manifest.json wins."""
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.agents import registry as agents_registry
    importlib.reload(agents_registry)

    user_dir = tmp_path / "agents" / "claude"
    user_dir.mkdir(parents=True)
    manifest = {
        "name": "claude",
        "description": "USER-OVERRIDDEN claude wrapper",
        "tool_names": ["claude_code"],
        "style": "wrapper",
    }
    (user_dir / "manifest.json").write_text(json.dumps(manifest))

    agents = agents_registry.list_agents()
    by_name = {a.name: a for a in agents}
    assert "claude" in by_name
    assert by_name["claude"].identity.description.startswith("USER-OVERRIDDEN")


def test_user_agent_malformed_manifest_skipped(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("JANUS_HOME", str(tmp_path))
    import importlib
    from janus import config
    importlib.reload(config)
    from janus.agents import registry as agents_registry
    importlib.reload(agents_registry)

    bad = tmp_path / "agents" / "broken"
    bad.mkdir(parents=True)
    (bad / "manifest.json").write_text("{ not valid json")

    # Should not raise. 'broken' simply doesn't appear in the list.
    names = [a.name for a in agents_registry.list_agents()]
    assert "broken" not in names


def test_slash_agent_handlers_dispatch():
    from janus.slash_dispatch import (
        SlashRegistry, SlashContext, register_shared_handlers,
    )
    reg = SlashRegistry()
    register_shared_handlers(reg)
    ctx = SlashContext(surface="test")

    handled, out = reg.dispatch("/agent list", ctx)
    assert handled is True
    assert "claude" in out

    handled, out = reg.dispatch("/agent", ctx)  # bare /agent → list
    assert handled is True
    assert "claude" in out

    handled, out = reg.dispatch("/claude", ctx)  # no arg → usage
    assert handled is True
    assert "usage" in out

    handled, out = reg.dispatch("/agent zzz-bad-name something", ctx)
    assert handled is True
    assert "not found" in out
