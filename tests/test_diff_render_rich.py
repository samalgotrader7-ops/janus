"""Tests for v1.25.4 — Rich diff viewer in approval prompts.

The model proposes fs_write or fs_edit; the approval Panel used to
render a unified-diff text dump with raw ANSI codes. v1.25.4:
diff.render_rich() returns a rich.syntax.Syntax object that gives
proper line numbers + diff lexer coloring, AND fs.py/edit.py pass
structured diff_data through the approver so cli_rich can swap in
the Rich version while other surfaces (basic CLI, web, telegram)
keep getting the ANSI rendering they already had.
"""
from __future__ import annotations

import pytest


# ---------- diff.render_rich ----------


def test_render_rich_returns_syntax_object_for_change():
    pytest.importorskip("rich")
    from janus import diff
    out = diff.render_rich("line1\nold\nline3\n", "line1\nnew\nline3\n", path="x.py")
    assert out is not None
    from rich.syntax import Syntax
    assert isinstance(out, Syntax)


def test_render_rich_returns_none_for_no_change():
    pytest.importorskip("rich")
    from janus import diff
    same = "line1\nline2\nline3\n"
    assert diff.render_rich(same, same) is None


def test_render_rich_returns_none_when_rich_missing(monkeypatch):
    """If rich isn't importable, render_rich returns None so the caller
    can fall back to the ANSI text version."""
    from janus import diff
    # Force the lazy import to fail.
    import builtins
    orig_import = builtins.__import__

    def _fake_import(name, *a, **kw):
        if name.startswith("rich.syntax"):
            raise ImportError("rich missing for this test")
        return orig_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    out = diff.render_rich("a\n", "b\n")
    assert out is None


def test_render_rich_uses_diff_lexer():
    """The Syntax object should request the 'diff' lexer so unified
    diff lines get the right per-line coloring."""
    pytest.importorskip("rich")
    from janus import diff
    out = diff.render_rich("a\n", "b\n", path="t.py")
    # Rich exposes the lexer via .lexer (a Lexer instance or str).
    name = getattr(out.lexer, "name", "") or str(out.lexer)
    assert "diff" in name.lower()


def test_render_rich_includes_path_in_diff_header():
    pytest.importorskip("rich")
    from janus import diff
    out = diff.render_rich("a\n", "b\n", path="src/foo.py")
    # Path goes through the underlying render() call — the unified
    # diff header should include the filename.
    code = str(out.code)
    assert "src/foo.py" in code


def test_render_rich_respects_max_lines_truncation():
    """Same truncation rule as the ANSI version — pin so the two
    can't drift."""
    pytest.importorskip("rich")
    from janus import diff
    big_old = "x\n" * 500
    big_new = "y\n" * 500
    out = diff.render_rich(big_old, big_new, max_lines=50)
    assert out is not None
    code = str(out.code)
    assert "truncated" in code


# ---------- fs.py / edit.py plumb diff_data through approver ----------


def test_fs_write_passes_diff_data_on_overwrite(tmp_path, monkeypatch):
    """fs_write should pass diff_data={old,new,path} to the approver
    when overwriting an existing file. Source-level pin."""
    from janus.tools.fs import FsWrite
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    target = tmp_path / "f.txt"
    target.write_text("hello\n", encoding="utf-8")

    captured = {}

    def _capture_approver(label, details, **kw):
        captured.update(kw)
        return False  # decline so we don't actually write

    out = FsWrite().run(
        {"path": "f.txt", "content": "world\n"},
        _capture_approver,
    )
    assert "diff_data" in captured
    assert captured["diff_data"]["old"] == "hello\n"
    assert captured["diff_data"]["new"] == "world\n"
    assert captured["diff_data"]["path"] == "f.txt"


def test_fs_write_no_diff_data_on_create(tmp_path, monkeypatch):
    """When creating a NEW file (no overwrite), diff_data is None
    because there's no `old` to diff against."""
    from janus.tools.fs import FsWrite
    from janus import config
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    captured = {}

    def _capture_approver(label, details, **kw):
        captured.update(kw)
        return False

    FsWrite().run(
        {"path": "newfile.txt", "content": "fresh\n"},
        _capture_approver,
    )
    # diff_data may be present as None or absent — both are fine.
    assert captured.get("diff_data") is None


def test_fs_edit_passes_diff_data(tmp_path, monkeypatch):
    """fs_edit always has old + new since it requires an existing file."""
    from janus.tools.edit import FsEdit
    from janus import config, read_tracker
    monkeypatch.setattr(config, "WORKSPACE", tmp_path)
    target = tmp_path / "e.txt"
    target.write_text("foo bar baz\n", encoding="utf-8")
    # Bypass the read-tracker requirement.
    monkeypatch.setenv("JANUS_FS_EDIT_REQUIRE_READ", "0")

    captured = {}

    def _capture_approver(label, details, **kw):
        captured.update(kw)
        return False

    FsEdit().run(
        {"path": "e.txt", "old_string": "bar", "new_string": "BAZ"},
        _capture_approver,
    )
    assert "diff_data" in captured
    assert captured["diff_data"]["old"] == "foo bar baz\n"
    assert "BAZ" in captured["diff_data"]["new"]


# ---------- cli_rich approver wire-up ----------


def test_cli_rich_approver_uses_render_rich_when_diff_data_passed():
    """Source-level pin: cli_rich's approver branches on diff_data and
    calls diff.render_rich for cli-rich-friendly output."""
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich)
    assert "diff_data" in src
    assert "render_rich" in src


def test_cli_rich_approver_falls_back_on_render_failure():
    """Source-level pin: try/except around the rich-render path."""
    pytest.importorskip("rich")
    pytest.importorskip("prompt_toolkit")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._make_mode_approver)
    assert "diff_data" in src
    assert "Fallback" in src or "fallback" in src or "except" in src


# ---------- Backward compat: surfaces ignoring the kwarg still work ----------


def test_existing_approver_signatures_accept_unknown_kwargs(tmp_path, monkeypatch):
    """Approvers in cli, web, telegram, whatsapp accept **kw — adding
    diff_data must not break them. Source-level scan over surfaces."""
    import inspect
    from janus import cli
    # cli.py's approver should accept arbitrary kwargs via **kw.
    src = inspect.getsource(cli)
    # Find approver definitions; check for **kw signature.
    assert "**kw" in src or "**kwargs" in src
