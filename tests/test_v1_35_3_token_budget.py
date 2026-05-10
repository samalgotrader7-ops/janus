"""Tests for v1.35.3 — token-budget compaction decision (Phase 9.2)."""

from __future__ import annotations

import pytest

from janus import token_budget as tb


def test_estimate_tokens_empty():
    assert tb.estimate_tokens("") == 0
    assert tb.estimate_tokens(None) == 0


def test_estimate_tokens_word_count_proxy():
    # 10 words × 1.3 = 13 tokens
    text = "this is a test sentence with exactly ten short words"
    assert tb.estimate_tokens(text) == 13


def test_estimate_messages_tokens_string_content():
    msgs = [
        {"role": "system", "content": "system prompt here"},  # 3 words → 3.9 ≈ 3
        {"role": "user", "content": "user message"},          # 2 words → 2
    ]
    total = tb.estimate_messages_tokens(msgs)
    # Don't assert exact — just bounds
    assert 2 <= total <= 10


def test_estimate_messages_tokens_block_shape():
    """apply_cache_markers wraps content in [{type: 'text', text: ...}]."""
    msgs = [{
        "role": "system",
        "content": [
            {"type": "text", "text": "system prompt"},
            {"type": "text", "text": "more content here"},
        ]
    }]
    assert tb.estimate_messages_tokens(msgs) > 0


def test_context_window_known_model():
    assert tb.context_window("claude-haiku-4-5") == 200_000
    assert tb.context_window("gpt-4o-mini") == 128_000


def test_context_window_strips_provider_prefix():
    assert tb.context_window("anthropic/claude-haiku-4-5") == 200_000
    assert tb.context_window("openai/gpt-4o") == 128_000


def test_context_window_unknown_falls_back_to_default():
    assert tb.context_window("some-unknown-model") == tb.DEFAULT_WINDOW


def test_context_window_env_override(monkeypatch):
    monkeypatch.setenv("JANUS_CONTEXT_WINDOW", "32000")
    assert tb.context_window("anything") == 32_000


def test_context_window_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("JANUS_CONTEXT_WINDOW", "not-a-number")
    assert tb.context_window("anything") == tb.DEFAULT_WINDOW


def test_compact_ratio_default():
    assert tb.compact_ratio() == 0.70


def test_compact_ratio_env_override(monkeypatch):
    monkeypatch.setenv("JANUS_COMPACT_RATIO", "0.85")
    assert tb.compact_ratio() == 0.85


def test_compact_ratio_clamps_to_valid_range(monkeypatch):
    monkeypatch.setenv("JANUS_COMPACT_RATIO", "5.0")
    assert tb.compact_ratio() == 0.99
    monkeypatch.setenv("JANUS_COMPACT_RATIO", "0.0")
    assert tb.compact_ratio() == 0.1


def test_should_compact_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JANUS_TOKEN_BUDGET_COMPACT", raising=False)
    # Even with huge content, should_compact returns False when off
    msgs = [{"role": "system", "content": "x" * 1_000_000}]
    assert tb.should_compact(msgs, "claude-haiku-4-5") is False


def test_should_compact_fires_above_threshold(monkeypatch):
    monkeypatch.setenv("JANUS_TOKEN_BUDGET_COMPACT", "1")
    monkeypatch.setenv("JANUS_COMPACT_RATIO", "0.5")
    monkeypatch.setenv("JANUS_CONTEXT_WINDOW", "1000")
    # 500+ tokens worth of content (1.3 * words ≈ tokens)
    big_text = " ".join(["word"] * 500)
    msgs = [{"role": "system", "content": big_text}]
    assert tb.should_compact(msgs, "any") is True


def test_should_compact_below_threshold(monkeypatch):
    monkeypatch.setenv("JANUS_TOKEN_BUDGET_COMPACT", "1")
    monkeypatch.setenv("JANUS_COMPACT_RATIO", "0.5")
    monkeypatch.setenv("JANUS_CONTEXT_WINDOW", "1000")
    msgs = [{"role": "system", "content": "tiny"}]
    assert tb.should_compact(msgs, "any") is False


def test_version_bumped_to_1_35_3_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 3)
