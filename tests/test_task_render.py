"""Tests for janus/task_render.py — Rich rendering of the agent's
todo list (v1.25.3 Phase 1b).

When the model calls todo_write/todo_read, cli_rich displays the
current state as a checklist panel instead of a one-line preview.
This file pins the rendering shape (icons, status, content),
on-disk read path, and the cli_rich wire-up.
"""
from __future__ import annotations

import json

import pytest


# ---------- Plain rendering ----------


def test_render_plain_empty():
    from janus import task_render
    out = task_render.render_plain([])
    assert "no todos" in out


def test_render_plain_single_pending():
    from janus import task_render
    out = task_render.render_plain([
        {"content": "do the thing", "status": "pending"},
    ])
    assert "○" in out
    assert "do the thing" in out


def test_render_plain_in_progress_marker():
    from janus import task_render
    out = task_render.render_plain([
        {"content": "in flight", "status": "in_progress"},
    ])
    assert "▶" in out


def test_render_plain_completed_marker():
    from janus import task_render
    out = task_render.render_plain([
        {"content": "done", "status": "completed"},
    ])
    assert "✓" in out


def test_render_plain_progress_header():
    from janus import task_render
    out = task_render.render_plain([
        {"content": "a", "status": "completed"},
        {"content": "b", "status": "completed"},
        {"content": "c", "status": "pending"},
    ])
    assert "2/3 done" in out


def test_render_plain_skips_empty_content():
    from janus import task_render
    out = task_render.render_plain([
        {"content": "", "status": "pending"},
        {"content": "real", "status": "pending"},
    ])
    assert "real" in out
    # Should only count items with content.
    assert "0/1 done" in out


def test_render_plain_unknown_status_treated_as_pending():
    from janus import task_render
    out = task_render.render_plain([
        {"content": "weird", "status": "made_up_status"},
    ])
    assert "○" in out  # pending icon


def test_render_plain_handles_non_dict_items():
    """Defensive: malformed list items shouldn't crash."""
    from janus import task_render
    # None, string, int — none of these have content; should all skip.
    out = task_render.render_plain([None, "string", 42, {"content": "ok"}])
    assert "ok" in out


# ---------- Rich panel ----------


def test_render_rich_panel_returns_object_when_rich_present():
    pytest.importorskip("rich")
    from janus import task_render
    panel = task_render.render_rich_panel([
        {"content": "x", "status": "pending"},
    ])
    assert panel is not None
    # Title carries the progress counter.
    assert "todos" in str(panel.title)


def test_render_rich_panel_empty_state_has_hint():
    pytest.importorskip("rich")
    from janus import task_render
    panel = task_render.render_rich_panel([])
    assert panel is not None
    # Just make sure it returns a Panel, not a crash. Hint text is
    # exercised via render_plain which shares the message contract.


def test_render_rich_panel_includes_in_progress_in_title():
    pytest.importorskip("rich")
    from janus import task_render
    panel = task_render.render_rich_panel([
        {"content": "active", "status": "in_progress"},
        {"content": "todo", "status": "pending"},
    ])
    assert "in progress" in str(panel.title)


def test_render_rich_panel_uses_brand_color():
    """Magenta border keeps the panel visually anchored as Janus output,
    not user output. Single source of truth in branding.BRAND_COLOR."""
    pytest.importorskip("rich")
    from janus import task_render
    panel = task_render.render_rich_panel([{"content": "x"}])
    # Rich Panel exposes border_style as an attribute (str or Style).
    assert "magenta" in str(panel.border_style)


# ---------- On-disk parser ----------


def test_parse_todos_from_disk_reads_json(tmp_path):
    from janus import task_render
    todos_file = tmp_path / "todos.json"
    todos_file.write_text(
        json.dumps([
            {"id": 0, "content": "x", "status": "pending"},
            {"id": 1, "content": "y", "status": "completed"},
        ]),
        encoding="utf-8",
    )
    todos = task_render.parse_todos_from_disk(todos_file)
    assert len(todos) == 2
    assert todos[0]["content"] == "x"


def test_parse_todos_from_disk_missing_file_returns_empty(tmp_path):
    from janus import task_render
    todos = task_render.parse_todos_from_disk(tmp_path / "nope.json")
    assert todos == []


def test_parse_todos_from_disk_corrupted_file_returns_empty(tmp_path):
    from janus import task_render
    bad = tmp_path / "bad.json"
    bad.write_text("not json {{{", encoding="utf-8")
    todos = task_render.parse_todos_from_disk(bad)
    assert todos == []


def test_parse_todos_from_disk_non_list_returns_empty(tmp_path):
    """If the file decoded but is a dict instead of a list, return []."""
    from janus import task_render
    p = tmp_path / "wrong.json"
    p.write_text(json.dumps({"oops": "not a list"}), encoding="utf-8")
    assert task_render.parse_todos_from_disk(p) == []


# ---------- cli_rich wire-up ----------


def test_cli_rich_renders_panel_for_todo_write():
    """Source-level pin: cli_rich's tool_result handler routes
    todo_write/todo_read through task_render."""
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "task_render" in src
    assert "todo_write" in src and "todo_read" in src


def test_cli_rich_falls_back_to_preview_on_render_failure():
    """If task_render somehow throws, cli_rich should fall back to the
    one-line preview rendering. Source-level pin."""
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    # Look for the except clause that prints the legacy preview as a
    # fallback when task_render fails.
    assert "except Exception" in src
    assert "result_preview" in src


# ---------- end-to-end smoke ----------


def test_interviews_categories_match_memory_cards_TYPES():
    """Pin the hardcoded SUPPORTED_CATEGORIES in interviews.py against
    memory_cards.TYPES so the two can't drift. v1.25.3 hardcoded
    SUPPORTED_CATEGORIES to break a pre-existing circular import; this
    test guards against future drift."""
    from janus import interviews, memory_cards
    assert interviews.SUPPORTED_CATEGORIES == tuple(memory_cards.TYPES)


def test_render_panel_via_disk_round_trip(tmp_path):
    """Full path: write todos to disk via janus.tools.todo, then render
    them via task_render. Catches drift between writer and renderer."""
    pytest.importorskip("rich")
    from janus import config, task_render
    from janus.tools.todo import TodoWrite

    # Point TODOS_FILE at our tmp file.
    todos_path = tmp_path / "todos.json"

    # Monkey-patch via setattr — cleaner than reload.
    orig = config.TODOS_FILE
    try:
        config.TODOS_FILE = todos_path
        config.HOME = tmp_path  # ensure_home target
        TodoWrite().run(
            {"todos": [
                {"content": "step 1", "status": "completed"},
                {"content": "step 2", "status": "in_progress"},
                {"content": "step 3", "status": "pending"},
            ]},
            lambda *a, **k: True,
        )
        todos = task_render.parse_todos_from_disk(todos_path)
        assert len(todos) == 3
        panel = task_render.render_rich_panel(todos)
        assert panel is not None
        assert "1/3 done" in str(panel.title)
        assert "1 in progress" in str(panel.title)
    finally:
        config.TODOS_FILE = orig
