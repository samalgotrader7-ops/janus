"""Tests for v1.34.9 — tool-schema slimming framework (Phase 9.5)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from janus import tool_schema_slim as tss


def _schema(name):
    return {"type": "function", "function": {"name": name, "parameters": {}}}


@dataclass
class FakeSkill:
    tool_names: list = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JANUS_TOOL_SCHEMA_SLIM", raising=False)
    assert tss.is_enabled() is False


def test_enabled_via_env_truthy(monkeypatch):
    for val in ("1", "true", "YES", "On"):
        monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", val)
        assert tss.is_enabled() is True


def test_select_returns_all_when_disabled(monkeypatch):
    monkeypatch.delenv("JANUS_TOOL_SCHEMA_SLIM", raising=False)
    schemas = [_schema("rare_tool"), _schema("fs_read")]
    out = tss.select_relevant(schemas)
    assert len(out) == 2


def test_select_keeps_always_include_when_enabled(monkeypatch):
    monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", "1")
    schemas = [
        _schema("fs_read"),         # always-include
        _schema("shell"),           # always-include
        _schema("rare_special"),    # not in always
        _schema("web_fetch"),       # always-include
    ]
    out = tss.select_relevant(schemas)
    names = {s["function"]["name"] for s in out}
    assert "fs_read" in names
    assert "shell" in names
    assert "web_fetch" in names
    assert "rare_special" not in names


def test_select_includes_skill_tool_names(monkeypatch):
    monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", "1")
    schemas = [_schema("fs_read"), _schema("rare_specific_tool")]
    skill = FakeSkill(tool_names=["rare_specific_tool"])
    out = tss.select_relevant(schemas, loaded_skills=[skill])
    names = {s["function"]["name"] for s in out}
    assert "rare_specific_tool" in names


def test_select_includes_skill_capability_keys(monkeypatch):
    monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", "1")
    schemas = [_schema("fs_read"), _schema("ssh_exec")]
    skill = FakeSkill(capabilities={"ssh_exec": ["*"]})
    out = tss.select_relevant(schemas, loaded_skills=[skill])
    names = {s["function"]["name"] for s in out}
    assert "ssh_exec" in names


def test_select_includes_recent_history(monkeypatch):
    monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", "1")
    schemas = [_schema("fs_read"), _schema("code_exec_python")]
    recent = [
        {"tool": "code_exec_python", "args": {}},
    ]
    out = tss.select_relevant(schemas, recent_records=recent)
    names = {s["function"]["name"] for s in out}
    assert "code_exec_python" in names


def test_recent_window_caps_to_last_8(monkeypatch):
    monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", "1")
    # Old tool 9+ records back should NOT be picked up
    recent = [{"tool": "ancient_tool"}] + [{"tool": "shell"}] * 8
    names = tss.collect_recent_tool_names(recent, window=8)
    assert "shell" in names
    assert "ancient_tool" not in names


def test_select_falls_back_when_slim_empty(monkeypatch):
    """If somehow nothing matches, fall back to ALL — model
    should never be starved of tools."""
    monkeypatch.setenv("JANUS_TOOL_SCHEMA_SLIM", "1")
    # No always-include matches in the schema list
    schemas = [_schema("xx"), _schema("yy")]
    out = tss.select_relevant(schemas)
    # Slim would be empty; defense gives us all
    assert len(out) == 2


def test_select_handles_empty_input():
    assert tss.select_relevant([]) == []


def test_collect_skill_tool_names_with_mcp_capability():
    """mcp.<server>.<tool> capability key produces mcp_<server>_<tool>
    name to match the registry naming."""
    skill = FakeSkill(capabilities={"mcp.git.diff": ["*"]})
    names = tss.collect_skill_tool_names([skill])
    assert "mcp_git_diff" in names


def test_collect_skill_tool_names_skips_partial_mcp_keys():
    """`mcp.<server>` (no specific tool) is too vague to predict
    which mcp_<server>_* the model will call — skip + rely on
    recent-history to catch it."""
    skill = FakeSkill(capabilities={"mcp.git": ["*"]})
    names = tss.collect_skill_tool_names([skill])
    # Should NOT speculatively add mcp_git_*
    assert not any(n.startswith("mcp_git") for n in names)


def test_always_include_set_pinned():
    """Pin the always-include list — adding/removing tools from
    here is a deliberate decision, not an accident."""
    expected_subset = {"fs_read", "shell", "web_fetch", "exit_plan_mode"}
    assert expected_subset.issubset(tss.ALWAYS_INCLUDE)


def test_version_bumped_to_1_34_9_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 9)
