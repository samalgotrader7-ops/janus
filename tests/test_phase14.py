"""Tests for Phase 14 — diff view, status line, streaming."""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from janus import diff, statusline, streaming, cost


# ---------- diff ----------


def test_diff_render_basic():
    out = diff.render("a\nb\nc\n", "a\nB\nc\n", path="x.txt", color=False)
    assert "x.txt" in out
    assert "-b" in out
    assert "+B" in out


def test_diff_render_no_change_returns_empty():
    out = diff.render("same\n", "same\n", color=False)
    assert out == ""


def test_diff_render_truncates_huge():
    old = "\n".join(f"line{i}" for i in range(500))
    new = "\n".join(f"X{i}" for i in range(500))
    out = diff.render(old, new, max_lines=50, color=False)
    assert "truncated" in out


def test_diff_render_color_wraps_added_removed():
    out = diff.render("a\n", "b\n", color=True)
    assert "\033[32m" in out  # green for +
    assert "\033[31m" in out  # red for -


def test_diff_stat_counts():
    s = diff.stat("a\nb\nc\n", "a\nb\nB\nc\n")
    # b → B + a new line means +2 -1 (or similar depending on opcode merging)
    assert s.startswith("+")
    assert "-" in s


def test_diff_stat_no_change():
    assert diff.stat("x", "x") == "no change"


# ---------- statusline ----------


def test_statusline_minimal():
    s = statusline.render(statusline.StatusInputs(model="m", turn=0))
    assert "model: m" in s


def test_statusline_includes_turn_and_flags():
    s = statusline.render(statusline.StatusInputs(
        model="m", turn=3, plan_on=True, parallel_on=True,
        verbose=True, conv_turns=5, skill="git-pr",
    ))
    assert "turn 3" in s
    assert "plan" in s
    assert "parallel" in s
    assert "verbose" in s
    assert "conv: 5 turns" in s
    assert "skill: git-pr" in s


def test_statusline_shows_cost_when_tracked():
    cost.reset_session()
    cost.record("openai/gpt-4o", {"prompt_tokens": 1000, "completion_tokens": 500})
    s = statusline.render(statusline.StatusInputs(model="openai/gpt-4o", turn=1))
    assert "tok" in s
    assert "$" in s
    cost.reset_session()


# ---------- streaming ----------


def _build_sse(chunks):
    """Format text chunks as OpenAI-shape SSE lines."""
    lines = []
    for text in chunks:
        body = json.dumps({"choices": [{"delta": {"content": text}, "index": 0}]})
        lines.append(f"data: {body}")
    lines.append("data: [DONE]")
    return lines


def test_chat_stream_yields_text_then_final_message(monkeypatch):
    sse = _build_sse(["Hello", " ", "world", "!"])

    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: fake_response
    fake_response.__exit__ = lambda *a: False
    fake_response.raise_for_status = lambda: None
    fake_response.iter_lines = lambda decode_unicode=True: iter(sse)

    with patch("janus.streaming.requests.post", return_value=fake_response):
        chunks = list(streaming.chat_stream([{"role": "user", "content": "x"}]))

    text_chunks = [c for c in chunks if isinstance(c, str)]
    final = [c for c in chunks if isinstance(c, dict)]
    assert "".join(text_chunks) == "Hello world!"
    assert len(final) == 1
    assert final[0]["content"] == "Hello world!"
    assert final[0]["role"] == "assistant"


def test_chat_stream_assembles_tool_calls_across_deltas(monkeypatch):
    """Tool calls arrive split across multiple deltas; we merge by index."""
    parts = [
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "id": "call_1", "function": {"name": "fs_re"}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "ad", "arguments": '{"path":'}}
        ]}}]},
        {"choices": [{"delta": {"tool_calls": [
            {"index": 0, "function": {"arguments": ' "x.py"}'}}
        ]}}]},
    ]
    sse = [f"data: {json.dumps(p)}" for p in parts] + ["data: [DONE]"]

    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: fake_response
    fake_response.__exit__ = lambda *a: False
    fake_response.raise_for_status = lambda: None
    fake_response.iter_lines = lambda decode_unicode=True: iter(sse)

    with patch("janus.streaming.requests.post", return_value=fake_response):
        chunks = list(streaming.chat_stream([{"role": "user", "content": "x"}]))
    final = chunks[-1]
    assert final["tool_calls"][0]["id"] == "call_1"
    assert final["tool_calls"][0]["function"]["name"] == "fs_read"
    assert final["tool_calls"][0]["function"]["arguments"] == '{"path": "x.py"}'


def test_chat_stream_records_usage_to_cost(monkeypatch):
    cost.reset_session()
    sse = [
        f'data: {json.dumps({"choices": [{"delta": {"content": "hi"}}]})}',
        f'data: {json.dumps({"usage": {"prompt_tokens": 10, "completion_tokens": 1}})}',
        "data: [DONE]",
    ]
    fake_response = MagicMock()
    fake_response.__enter__ = lambda s: fake_response
    fake_response.__exit__ = lambda *a: False
    fake_response.raise_for_status = lambda: None
    fake_response.iter_lines = lambda decode_unicode=True: iter(sse)

    with patch("janus.streaming.requests.post", return_value=fake_response):
        list(streaming.chat_stream([{"role": "user", "content": "x"}]))
    assert cost.session_stats().prompt_tokens == 10
    assert cost.session_stats().completion_tokens == 1
    cost.reset_session()
