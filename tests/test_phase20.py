"""Tests for Phase 20 — provider niceties (cache markers + local model)."""
from __future__ import annotations
import json
from unittest.mock import patch, MagicMock

import pytest

from janus import config, llm, cost


# ---------- cache markers ----------


def test_cache_markers_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", False)
    msgs = [{"role": "system", "content": "system text"},
            {"role": "user", "content": "hi"}]
    out = llm.apply_cache_markers(msgs)
    # No transformation — same shapes back.
    assert out == msgs
    assert isinstance(out[0]["content"], str)


def test_cache_markers_on_wraps_last_system_message(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [
        {"role": "system", "content": "first system"},
        {"role": "user", "content": "hi"},
        {"role": "system", "content": "second system (this one wraps)"},
        {"role": "user", "content": "more"},
    ]
    out = llm.apply_cache_markers(msgs)
    # First system stays a string.
    assert isinstance(out[0]["content"], str)
    # Second (last) system becomes a content block with cache_control.
    last = out[2]
    assert isinstance(last["content"], list)
    assert last["content"][0]["text"] == "second system (this one wraps)"
    assert last["content"][0]["cache_control"] == {"type": "ephemeral"}


def test_cache_markers_skip_when_no_system_message(monkeypatch):
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [{"role": "user", "content": "no system here"}]
    assert llm.apply_cache_markers(msgs) == msgs


def test_cache_markers_skip_when_already_a_list(monkeypatch):
    """Caller built blocks themselves — leave them alone."""
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    msgs = [{"role": "system", "content": [{"type": "text", "text": "x"}]}]
    out = llm.apply_cache_markers(msgs)
    assert out == msgs


def test_chat_includes_markers_in_payload(monkeypatch):
    """End-to-end: with the flag on, the JSON sent on the wire carries
    the wrapped system message."""
    monkeypatch.setattr(config, "PROMPT_CACHE_MARKERS", True)
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured["json"] = json
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {
            "choices": [{"message": {"role": "assistant", "content": "ok"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
        return resp

    with patch("janus.llm.requests.post", side_effect=fake_post):
        llm.chat([
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U"},
        ])
    sent = captured["json"]["messages"]
    assert isinstance(sent[0]["content"], list)
    assert sent[0]["content"][0]["cache_control"] == {"type": "ephemeral"}


# ---------- local-model robustness ----------


def test_local_provider_no_usage_doesnt_crash(monkeypatch):
    """Some local providers (Ollama, llama.cpp) don't return `usage`.
    cost.record must be a no-op; llm.chat must not crash."""
    cost.reset_session()

    def fake_post(url, headers=None, json=None, timeout=None):
        resp = MagicMock()
        resp.raise_for_status = lambda: None
        resp.json = lambda: {
            "choices": [{"message": {"role": "assistant", "content": "hi"}}],
            # no `usage` field
        }
        return resp

    with patch("janus.llm.requests.post", side_effect=fake_post):
        msg = llm.chat([{"role": "user", "content": "x"}])
    assert msg["content"] == "hi"
    # No tokens recorded, no error.
    assert cost.session_stats().prompt_tokens == 0
    cost.reset_session()


def test_local_model_zero_priced_in_table():
    """Ensure the price table treats `local/*` as free so user isn't told
    they spent $X on a local Ollama call."""
    usd = cost.estimate_usd("local/llama", 1_000_000, 1_000_000)
    assert usd == 0.0


def test_unknown_local_model_normalizes_via_suffix(monkeypatch):
    """`estimate_usd` falls back to suffix match for unknown prefixes:
    e.g. `mycompany/gpt-4o-mini` matches `openai/gpt-4o-mini`'s rate."""
    usd = cost.estimate_usd("mycompany/gpt-4o-mini", 1_000_000, 0)
    # Suffix `gpt-4o-mini` matches openai/gpt-4o-mini ($0.15/M input).
    assert usd == pytest.approx(0.15)
