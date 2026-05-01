"""Tests for Phase 13 — conversation persistence + recap + compaction."""
from __future__ import annotations

import json

import pytest

from janus import config, conversation


def test_new_conversation_has_id_and_zero_turns(janus_home):
    c = conversation.new()
    assert c.id
    assert c.turns == []
    assert c.summary == ""


def test_save_and_load_roundtrip(janus_home):
    c = conversation.new()
    c.add_turn(request="hello", output="hi back")
    conversation.save(c)
    reloaded = conversation.load(c.id)
    assert reloaded is not None
    assert reloaded.id == c.id
    assert len(reloaded.turns) == 1
    assert reloaded.turns[0]["request"] == "hello"


def test_load_missing_returns_none(janus_home):
    assert conversation.load("does-not-exist") is None


def test_list_all_newest_first(janus_home):
    a = conversation.new(); a.add_turn(request="A", output="a")
    a.last_updated = "2026-04-30T10:00:00+00:00"; conversation.save(a)
    b = conversation.new(); b.add_turn(request="B", output="b")
    b.last_updated = "2026-05-01T10:00:00+00:00"; conversation.save(b)
    items = conversation.list_all()
    assert items[0]["id"] == b.id  # newer first
    assert items[1]["id"] == a.id


def test_latest_returns_most_recent(janus_home):
    a = conversation.new(); a.add_turn(request="A", output="a")
    a.last_updated = "2026-04-30T10:00:00+00:00"; conversation.save(a)
    b = conversation.new(); b.add_turn(request="B", output="b")
    b.last_updated = "2026-05-01T10:00:00+00:00"; conversation.save(b)
    latest = conversation.latest()
    assert latest is not None and latest.id == b.id


def test_recent_context_block_empty_when_no_turns(janus_home):
    c = conversation.new()
    assert c.recent_context_block() == ""


def test_recent_context_block_includes_last_k_turns(janus_home):
    c = conversation.new()
    for i in range(8):
        c.add_turn(request=f"q{i}", output=f"a{i}")
    block = c.recent_context_block(k=3)
    # Only last 3 turns referenced.
    assert "q7" in block
    assert "q6" in block
    assert "q5" in block
    assert "q4" not in block


def test_recent_context_block_includes_summary(janus_home):
    c = conversation.new()
    c.summary = "Earlier the user worked on the parser."
    block = c.recent_context_block()
    assert "Earlier in this session" in block
    assert "parser" in block


def test_clear_turns_resets_turns_and_summary(janus_home):
    c = conversation.new()
    c.add_turn(request="x", output="y")
    c.summary = "stuff"
    c.clear_turns()
    assert c.turns == []
    assert c.summary == ""


def test_compact_below_threshold_is_noop(janus_home):
    c = conversation.new()
    c.add_turn(request="x", output="y")  # only 1 turn, default keep_last=3
    conversation.compact(c)
    assert len(c.turns) == 1
    assert c.summary == ""


def test_compact_replaces_old_turns_with_summary(janus_home, fake_llm):
    c = conversation.new()
    for i in range(10):
        c.add_turn(request=f"q{i}", output=f"a{i}")
    fake_llm.append({
        "content": "User worked on parsing tasks q0-q6, then moved to refactoring.",
        "role": "assistant",
    })
    conversation.compact(c, keep_last=3)
    # Most recent 3 kept.
    assert len(c.turns) == 3
    assert c.turns[-1]["request"] == "q9"
    assert c.turns[0]["request"] == "q7"
    # Summary populated.
    assert "parsing" in c.summary or "refactoring" in c.summary


def test_set_pending_take_pending_handoff(janus_home):
    c = conversation.new()
    conversation.set_pending(c)
    assert conversation.take_pending() is c
    # take_pending consumes — second call returns None.
    assert conversation.take_pending() is None


def test_new_id_format():
    a = conversation.new_id()
    b = conversation.new_id()
    # ISO timestamp + dash + 4 hex chars.
    assert a != b
    assert a.endswith(tuple("0123456789abcdef"))
    assert "-" in a
