"""Tests for Phase 9 — tool surface expansion.

Covers each new tool with at least one happy path and one
refuse-on-escape (workspace breakout, approval denial, AST violation,
or missing-dep) per the build guide §6 Phase 9 acceptance criterion.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path

import pytest

from janus import config, logger
from janus.tools import default_registry
from janus.tools.edit import FsEdit
from janus.tools.multi_edit import FsMultiEdit
from janus.tools.glob import FsGlob
from janus.tools.grep import FsGrep
from janus.tools.todo import TodoRead, TodoWrite
from janus.tools.session_search import SessionSearch, SessionRecent
from janus.tools.code_exec import CodeExecPython, ast_check
from janus.tools.notebook import NbRead, NbEdit
from janus.tools.web_search import WebSearch
from janus.tools.browser import BrowserNavigate
from janus.tools.vision import ImageDescribe


def _approve(*a, **kw):
    return True


def _deny(*a, **kw):
    return False


# ---------- Default registry ----------


def test_default_registry_includes_all_phase9_tools(janus_home):
    reg = default_registry()
    names = set(reg.names())
    expected_phase9 = {
        "fs_edit", "fs_multi_edit", "fs_glob", "fs_grep",
        "todo_read", "todo_write",
        "session_search", "session_recent",
        "code_exec_python",
        "nb_read", "nb_edit",
        "web_search",
        "browser_navigate", "browser_text", "browser_snapshot",
        "browser_links", "browser_get_image",
        "image_describe",
    }
    missing = expected_phase9 - names
    assert not missing, f"missing tools: {missing}"


# ---------- fs_edit ----------


def test_fs_edit_happy_path(janus_home):
    # v1.15.0: fs_edit now requires a prior fs_read in the same session.
    from janus.tools.fs import FsRead
    p = config.WORKSPACE / "x.txt"
    p.write_text("hello world", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "world", "new_string": "janus"},
        _approve,
    )
    assert "edited" in out
    assert p.read_text(encoding="utf-8") == "hello janus"


def test_fs_edit_refuses_workspace_escape(janus_home):
    out = FsEdit().run(
        {"path": "../escape.txt", "old_string": "x", "new_string": "y"},
        _approve,
    )
    assert out.startswith("error:") and "outside workspace" in out


def test_fs_edit_refuses_when_old_string_ambiguous(janus_home):
    from janus.tools.fs import FsRead
    p = config.WORKSPACE / "x.txt"
    p.write_text("foo foo foo", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "foo", "new_string": "bar"},
        _approve,
    )
    assert "occurs 3 times" in out
    assert p.read_text(encoding="utf-8") == "foo foo foo"


def test_fs_edit_replace_all_succeeds_on_ambiguous(janus_home):
    from janus.tools.fs import FsRead
    p = config.WORKSPACE / "x.txt"
    p.write_text("foo foo foo", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "foo", "new_string": "bar",
         "replace_all": True},
        _approve,
    )
    assert "edited" in out
    assert p.read_text(encoding="utf-8") == "bar bar bar"


def test_fs_edit_refuses_on_user_denial(janus_home):
    from janus.tools.fs import FsRead
    p = config.WORKSPACE / "x.txt"
    p.write_text("hello", encoding="utf-8")
    FsRead().run({"path": "x.txt"}, _approve)
    out = FsEdit().run(
        {"path": "x.txt", "old_string": "hello", "new_string": "bye"},
        _deny,
    )
    assert out.startswith("refused")
    assert p.read_text(encoding="utf-8") == "hello"


# ---------- fs_multi_edit ----------


def test_fs_multi_edit_atomic_happy(janus_home):
    a = config.WORKSPACE / "a.txt"; a.write_text("aaa", encoding="utf-8")
    b = config.WORKSPACE / "b.txt"; b.write_text("bbb", encoding="utf-8")
    out = FsMultiEdit().run(
        {"edits": [
            {"path": "a.txt", "old_string": "aaa", "new_string": "AAA"},
            {"path": "b.txt", "old_string": "bbb", "new_string": "BBB"},
        ]},
        _approve,
    )
    assert "applied 2 edit(s) across 2 file(s)" in out
    assert a.read_text(encoding="utf-8") == "AAA"
    assert b.read_text(encoding="utf-8") == "BBB"


def test_fs_multi_edit_aborts_if_any_preflight_fails(janus_home):
    a = config.WORKSPACE / "a.txt"; a.write_text("aaa", encoding="utf-8")
    b = config.WORKSPACE / "b.txt"; b.write_text("bbb", encoding="utf-8")
    out = FsMultiEdit().run(
        {"edits": [
            {"path": "a.txt", "old_string": "aaa", "new_string": "AAA"},
            # b: old_string not found → pre-flight fails → no writes.
            {"path": "b.txt", "old_string": "missing", "new_string": "X"},
        ]},
        _approve,
    )
    assert out.startswith("error:")
    assert a.read_text(encoding="utf-8") == "aaa"  # rolled back / never written
    assert b.read_text(encoding="utf-8") == "bbb"


def test_fs_multi_edit_refuses_on_denial(janus_home):
    a = config.WORKSPACE / "a.txt"; a.write_text("aaa", encoding="utf-8")
    out = FsMultiEdit().run(
        {"edits": [{"path": "a.txt", "old_string": "aaa", "new_string": "X"}]},
        _deny,
    )
    assert out.startswith("refused")
    assert a.read_text(encoding="utf-8") == "aaa"


# ---------- fs_glob ----------


def test_fs_glob_matches(janus_home):
    (config.WORKSPACE / "src").mkdir()
    (config.WORKSPACE / "src" / "a.py").write_text("", encoding="utf-8")
    (config.WORKSPACE / "src" / "b.py").write_text("", encoding="utf-8")
    out = FsGlob().run({"pattern": "src/*.py"}, _approve)
    assert "src/a.py" in out and "src/b.py" in out


def test_fs_glob_no_matches(janus_home):
    out = FsGlob().run({"pattern": "*.nonexistent"}, _approve)
    assert "no matches" in out


# ---------- fs_grep ----------


def test_fs_grep_finds_matches(janus_home):
    (config.WORKSPACE / "x.txt").write_text("foo\nbar baz\nfoo again\n", encoding="utf-8")
    out = FsGrep().run({"pattern": "foo"}, _approve)
    assert "foo" in out
    assert "x.txt" in out


def test_fs_grep_no_matches(janus_home):
    (config.WORKSPACE / "x.txt").write_text("just text", encoding="utf-8")
    out = FsGrep().run({"pattern": "nonexistent_pattern_xyz"}, _approve)
    assert "no matches" in out


def test_fs_grep_refuses_workspace_escape(janus_home):
    out = FsGrep().run({"pattern": "x", "path": "../"}, _approve)
    assert out.startswith("error:") and "outside workspace" in out


# ---------- todo ----------


def test_todo_round_trip(janus_home):
    write_out = TodoWrite().run(
        {"todos": [
            {"content": "do A", "status": "pending"},
            {"content": "do B", "status": "in_progress"},
        ]},
        _approve,
    )
    assert "saved 2 todo" in write_out

    read_out = TodoRead().run({}, _approve)
    assert "do A" in read_out
    assert "do B" in read_out
    assert "[in_progress]" in read_out


def test_todo_read_empty(janus_home):
    out = TodoRead().run({}, _approve)
    assert out == "(no todos)"


def test_todo_write_normalizes_invalid_status(janus_home):
    TodoWrite().run(
        {"todos": [{"content": "x", "status": "garbage"}]},
        _approve,
    )
    out = TodoRead().run({}, _approve)
    assert "[pending]" in out


# ---------- session_search / session_recent ----------


def test_session_recent_empty(janus_home):
    out = SessionRecent().run({}, _approve)
    assert "no recent" in out or out == "(no recent records)"


def test_session_search_finds_logged_record(janus_home):
    # Seed a record then query.
    logger.write({
        "ts": "2026-04-30T12:00:00Z",
        "request": "find me later please",
        "interpretations": [{"label": "x", "action": "x", "risk": "low"}],
        "choice": 1,
    })
    out = SessionSearch().run({"query": "find"}, _approve)
    assert "find me later please" in out


# ---------- code_exec_python ----------


def test_code_exec_ast_refuses_os_import():
    assert ast_check("import os") is not None
    assert ast_check("import os") and "forbidden" in ast_check("import os")


def test_code_exec_ast_refuses_subprocess_import():
    assert ast_check("import subprocess") is not None


def test_code_exec_ast_refuses_dunder_globals():
    assert ast_check("().__class__.__bases__") is not None


def test_code_exec_ast_refuses_eval_call():
    assert ast_check("eval('1+1')") is not None


def test_code_exec_ast_accepts_clean_code():
    assert ast_check("x = sum(range(10)); print(x)") is None


def test_code_exec_python_runs_clean_code(janus_home):
    out = CodeExecPython().run(
        {"code": "print(2+2)"},
        _approve,
    )
    assert "exit=0" in out
    assert "4" in out


def test_code_exec_python_refuses_forbidden_code(janus_home):
    out = CodeExecPython().run(
        {"code": "import os; print(os.environ.get('JANUS_API_KEY'))"},
        _approve,
    )
    assert out.startswith("refused (AST pre-flight)")
    # Verify it never reached the subprocess (no exit= line).
    assert "exit=" not in out


def test_code_exec_python_refuses_on_user_denial(janus_home):
    out = CodeExecPython().run({"code": "print(1)"}, _deny)
    assert out.startswith("refused by user")


# ---------- nb_read / nb_edit ----------


_NB_FIXTURE = {
    "cells": [
        {"cell_type": "markdown", "source": "# Title", "metadata": {}},
        {"cell_type": "code", "source": "print('hi')",
         "metadata": {}, "execution_count": None, "outputs": []},
    ],
    "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
}


def _write_nb(janus_home, name="nb.ipynb", data=None):
    p = config.WORKSPACE / name
    p.write_text(json.dumps(data or _NB_FIXTURE, indent=1), encoding="utf-8")
    return p


def test_nb_read_dumps_cells(janus_home):
    _write_nb(janus_home)
    out = NbRead().run({"path": "nb.ipynb"}, _approve)
    assert "[markdown]" in out
    assert "[code]" in out
    assert "# Title" in out
    assert "print('hi')" in out


def test_nb_read_refuses_escape(janus_home):
    out = NbRead().run({"path": "../escape.ipynb"}, _approve)
    assert out.startswith("error:") and "outside workspace" in out


def test_nb_edit_replaces_cell(janus_home):
    p = _write_nb(janus_home)
    out = NbEdit().run(
        {"path": "nb.ipynb", "cell_index": 1,
         "operation": "replace", "source": "print('replaced')"},
        _approve,
    )
    assert "applied replace" in out
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["cells"][1]["source"] == "print('replaced')"


def test_nb_edit_inserts_cell(janus_home):
    p = _write_nb(janus_home)
    NbEdit().run(
        {"path": "nb.ipynb", "cell_index": 1,
         "operation": "insert", "source": "x = 1", "cell_type": "code"},
        _approve,
    )
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["cells"]) == 3
    assert data["cells"][1]["source"] == "x = 1"


def test_nb_edit_refuses_on_denial(janus_home):
    p = _write_nb(janus_home)
    out = NbEdit().run(
        {"path": "nb.ipynb", "cell_index": 0,
         "operation": "delete"},
        _deny,
    )
    assert out.startswith("refused")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["cells"]) == 2


# ---------- web_search ----------


def test_web_search_no_api_key_returns_clear_error(janus_home, monkeypatch):
    monkeypatch.setattr(config, "BRAVE_API_KEY", "")
    out = WebSearch().run({"query": "anything"}, _approve)
    assert out.startswith("error:") and "JANUS_BRAVE_API_KEY" in out


def test_web_search_unknown_provider_returns_error(janus_home, monkeypatch):
    monkeypatch.setattr(config, "WEB_SEARCH_PROVIDER", "made-up-engine")
    out = WebSearch().run({"query": "x"}, _approve)
    assert "unknown web search provider" in out


# ---------- browser ----------


def test_browser_navigate_without_playwright_returns_clear_error(janus_home, monkeypatch):
    """If playwright isn't installed, every browser tool returns a clear hint."""
    import janus.tools.browser as br
    monkeypatch.setattr(br, "_try_import_playwright", lambda: None)
    out = BrowserNavigate().run({"url": "https://example.com"}, _approve)
    assert "playwright not installed" in out


# ---------- vision ----------


def test_image_describe_refuses_workspace_escape(janus_home):
    out = ImageDescribe().run({"path": "../etc/passwd"}, _approve)
    assert out.startswith("error:") and "outside workspace" in out


def test_image_describe_refuses_unsupported_extension(janus_home):
    p = config.WORKSPACE / "x.txt"
    p.write_text("not an image", encoding="utf-8")
    out = ImageDescribe().run({"path": "x.txt"}, _approve)
    assert "unsupported image type" in out


def test_image_describe_routes_through_llm(janus_home, fake_llm):
    p = config.WORKSPACE / "tiny.png"
    # Minimal valid PNG (1x1 transparent).
    png_bytes = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
        "0000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082"
    )
    p.write_bytes(png_bytes)
    fake_llm.append({"content": "A 1x1 transparent pixel.", "role": "assistant"})
    out = ImageDescribe().run({"path": "tiny.png"}, _approve)
    assert "transparent" in out
