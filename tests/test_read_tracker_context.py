"""Tests for v1.25.6 — read-once context awareness.

read_tracker has tracked which files the model fs_read in this session
since v1.15. v1.25.6 surfaces that list to the model at turn start as
a context block, so the model stops re-reading files it already has.

Companion to Rule 22 (don't spelunk source for explanation Qs) — that
rule says "don't"; this gives the model concrete evidence of what's
already been seen.
"""
from __future__ import annotations

import pytest


# ---------- context_summary ----------


def _reset(monkeypatch):
    """Drop the thread-local read state between tests."""
    from janus import read_tracker
    read_tracker.reset()


def test_context_summary_empty_when_no_reads(monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    assert read_tracker.context_summary() == ""


def test_context_summary_lists_read_files(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    f = tmp_path / "x.py"
    f.write_text("hello\n")
    read_tracker.mark_read(f)
    out = read_tracker.context_summary(workspace=str(tmp_path))
    assert "x.py" in out
    assert "Files already in your session context" in out


def test_context_summary_renders_relative_to_workspace(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    sub = tmp_path / "src"
    sub.mkdir()
    f = sub / "foo.py"
    f.write_text("x\n")
    read_tracker.mark_read(f)
    out = read_tracker.context_summary(workspace=str(tmp_path))
    # Relative form should appear; absolute form should not.
    assert "src/foo.py" in out or "src\\foo.py" in out
    # Absolute path component shouldn't be in the rendered list.
    abs_part = str(tmp_path)
    # On Windows the absolute path may contain :, on POSIX /
    # We allow the workspace root in the header but not as a prefix
    # for the listed file. The strict assertion: no "abs/sub/foo.py"
    # form.
    assert f"{abs_part}/src/foo.py" not in out
    assert f"{abs_part}\\src\\foo.py" not in out


def test_context_summary_renders_absolute_when_outside_workspace(tmp_path, monkeypatch):
    """A file read from outside the workspace (e.g. ~/.janus/) should
    still be listed, with its absolute path."""
    _reset(monkeypatch)
    from janus import read_tracker
    ws = tmp_path / "ws"
    ws.mkdir()
    other = tmp_path / "outside.py"
    other.write_text("hi\n")
    read_tracker.mark_read(other)
    out = read_tracker.context_summary(workspace=str(ws))
    assert str(other) in out or "outside.py" in out


def test_context_summary_includes_size_marker(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    f = tmp_path / "small.py"
    # write_bytes to dodge platform-specific newline expansion (Windows
    # write_text expands LF → CRLF, blowing up the size by 50%).
    f.write_bytes(b"a\nb\nc\n")
    read_tracker.mark_read(f)
    out = read_tracker.context_summary(workspace=str(tmp_path))
    # 6 bytes → "6B"; small file
    assert "6B" in out


def test_context_summary_kilobyte_size_marker(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    f = tmp_path / "biggish.py"
    f.write_text("x" * 5000)
    read_tracker.mark_read(f)
    out = read_tracker.context_summary(workspace=str(tmp_path))
    # 5000B → 4KB
    assert "4KB" in out


def test_context_summary_caps_at_max_paths(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    for i in range(40):
        p = tmp_path / f"f{i:02d}.py"
        p.write_text("x\n")
        read_tracker.mark_read(p)
    out = read_tracker.context_summary(
        workspace=str(tmp_path), max_paths=10,
    )
    # Should include the truncation marker.
    assert "more" in out
    # At most 10 path lines + marker.
    lines = [ln for ln in out.split("\n") if ln.startswith("- ")]
    assert len(lines) <= 11  # 10 paths + 1 truncation


def test_context_summary_alphabetical_order(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    for name in ("zebra.py", "alpha.py", "mango.py"):
        p = tmp_path / name
        p.write_text("x\n")
        read_tracker.mark_read(p)
    out = read_tracker.context_summary(workspace=str(tmp_path))
    a_pos = out.find("alpha.py")
    m_pos = out.find("mango.py")
    z_pos = out.find("zebra.py")
    assert -1 < a_pos < m_pos < z_pos


def test_context_summary_includes_dont_re_read_hint(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import read_tracker
    f = tmp_path / "x.py"
    f.write_text("hi\n")
    read_tracker.mark_read(f)
    out = read_tracker.context_summary(workspace=str(tmp_path))
    # The block must include actionable guidance, not just a list.
    assert "fs_read" in out
    assert "context" in out.lower()


# ---------- executor wire-up ----------


def test_build_chat_system_includes_context_block_when_files_read(tmp_path, monkeypatch):
    """The executor's system prompt builder injects the context summary
    between memory_preamble and JANUS_CHAT_SYSTEM when there are reads."""
    _reset(monkeypatch)
    from janus import executor, read_tracker
    f = tmp_path / "x.py"
    f.write_text("y\n")
    read_tracker.mark_read(f)
    sysprompt = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="some memory",
        skill_body="",
    )
    assert "Files already in your session context" in sysprompt
    assert "x.py" in sysprompt


def test_build_chat_system_omits_context_block_when_no_reads(tmp_path, monkeypatch):
    _reset(monkeypatch)
    from janus import executor
    sysprompt = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="some memory",
        skill_body="",
    )
    assert "Files already in your session context" not in sysprompt


def test_build_chat_system_context_block_after_memory_before_rules(tmp_path, monkeypatch):
    """The block must appear AFTER memory_preamble (which contains
    typed cards + legacy md) and BEFORE JANUS_CHAT_SYSTEM (the rules)."""
    _reset(monkeypatch)
    from janus import executor, read_tracker
    f = tmp_path / "x.py"
    f.write_text("y\n")
    read_tracker.mark_read(f)
    sysprompt = executor._build_chat_system(
        workspace=str(tmp_path),
        mode="default",
        memory_preamble="MEMORY_MARKER_AAA",
        skill_body="",
    )
    mem_pos = sysprompt.find("MEMORY_MARKER_AAA")
    ctx_pos = sysprompt.find("Files already in your session context")
    rule_pos = sysprompt.find("EXPLANATION QUESTIONS")  # Rule 22 anchor
    assert mem_pos != -1 and ctx_pos != -1 and rule_pos != -1
    assert mem_pos < ctx_pos < rule_pos
