"""Tests for v1.35.4 — event-to-display renderer (Phase 9.1)."""

from __future__ import annotations

import pytest

from janus.event_render import render_event, RenderedEvent


def test_empty_event():
    r = render_event(None)
    assert r.kind == "unknown"


def test_tool_call_renders_name_and_args():
    e = {"type": "tool_call", "payload": {"tool": "fs_read", "args": {"path": "/etc/hosts"}}}
    r = render_event(e)
    assert r.kind == "tool"
    assert r.glyph == "🔧"
    assert "fs_read" in r.line
    assert "path=" in r.detail


def test_tool_result_success_glyph():
    e = {"type": "tool_result", "payload": {"tool": "shell", "result_preview": "ok"}}
    r = render_event(e)
    assert r.glyph == "✓"


def test_tool_result_error_glyph():
    e = {"type": "tool_result", "payload": {"tool": "shell", "result_preview": "error: command failed"}}
    r = render_event(e)
    assert r.glyph == "✗"


def test_skill_loaded_with_state():
    e = {"type": "skill_loaded", "payload": {"name": "git-pr", "state": "trusted"}}
    r = render_event(e)
    assert r.kind == "skill"
    assert r.glyph == "📚"
    assert "git-pr" in r.line
    assert "trusted" in r.line


def test_memory_update_with_summary():
    e = {"type": "memory_update", "payload": {"op_count": 3, "summary": "added project info"}}
    r = render_event(e)
    assert r.kind == "memory"
    assert "3" in r.line
    assert "added project info" in r.line


def test_memory_recall():
    e = {"type": "memory_recall", "payload": {"count": 5}}
    r = render_event(e)
    assert r.kind == "memory"
    assert "5" in r.line


def test_thinking_with_note():
    e = {"type": "thinking", "payload": {"note": "considering options"}}
    r = render_event(e)
    assert r.kind == "thinking"
    assert "considering options" in r.line


def test_subagent_start():
    e = {"type": "subagent_start", "payload": {"description": "find all .py files"}}
    r = render_event(e)
    assert r.kind == "subagent"
    assert "find all" in r.line


def test_subagent_end_success_vs_failure():
    ok = render_event({"type": "subagent_end", "payload": {"success": True}})
    fail = render_event({"type": "subagent_end", "payload": {"success": False}})
    assert "done" in ok.line
    assert "failed" in fail.line


def test_hook_fired():
    e = {"type": "hook_fired", "payload": {"name": "pre-tool"}}
    r = render_event(e)
    assert r.kind == "system"
    assert "pre-tool" in r.line


def test_budget_alert():
    e = {"type": "budget_alert", "payload": {"percent": 80}}
    r = render_event(e)
    assert "80" in r.line


def test_verification_result_pass_fail():
    p = render_event({"type": "verification_result", "payload": {"passed": True}})
    f = render_event({"type": "verification_result", "payload": {"passed": False}})
    assert "passed" in p.line
    assert "failed" in f.line


def test_mode_change():
    e = {"type": "mode_change", "payload": {"from_mode": "plan", "to_mode": "default"}}
    r = render_event(e)
    assert "plan" in r.line
    assert "default" in r.line


def test_unknown_event_safe():
    e = {"type": "fictional_event_type_xyz", "payload": {"x": 1}}
    r = render_event(e)
    assert r.kind == "unknown"


def test_truncates_long_args():
    """Detail field should truncate to ~200 chars."""
    big = "x" * 1000
    e = {"type": "tool_call", "payload": {"tool": "shell", "args": {"command": big}}}
    r = render_event(e)
    assert len(r.detail) <= 210


def test_long_tool_args_value_truncated_individually():
    """Each arg value gets truncated to 30 chars in the rendered detail."""
    e = {"type": "tool_call", "payload": {
        "tool": "shell", "args": {"path": "x" * 100},
    }}
    r = render_event(e)
    # The arg value is truncated to 30 chars + ellipsis
    assert len(r.detail) < 80


def test_version_bumped_to_1_35_4_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 4)
