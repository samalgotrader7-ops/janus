"""Tests for Phase 13 — cost / token tracker."""
from __future__ import annotations

import json

import pytest

from janus import cost, config


@pytest.fixture(autouse=True)
def reset_cost():
    cost.reset_session()
    yield
    cost.reset_session()


def test_record_accumulates_per_turn_and_session():
    cost.new_turn()
    cost.record("openai/gpt-4o", {"prompt_tokens": 100, "completion_tokens": 50})
    cost.record("openai/gpt-4o", {"prompt_tokens": 200, "completion_tokens": 80})
    t = cost.turn_stats()
    s = cost.session_stats()
    assert t.prompt_tokens == 300
    assert t.completion_tokens == 130
    assert s.prompt_tokens == 300
    assert s.completion_tokens == 130
    # Two LLM calls.
    assert t.calls == 2 and s.calls == 2


def test_new_turn_resets_turn_only():
    cost.record("openai/gpt-4o", {"prompt_tokens": 50, "completion_tokens": 25})
    cost.new_turn()
    cost.record("openai/gpt-4o", {"prompt_tokens": 10, "completion_tokens": 5})
    assert cost.turn_stats().prompt_tokens == 10
    assert cost.session_stats().prompt_tokens == 60


def test_estimate_usd_known_model():
    # gpt-4o: $5/M input, $15/M output. 1M input + 1M output = $5 + $15 = $20.
    usd = cost.estimate_usd("openai/gpt-4o", 1_000_000, 1_000_000)
    assert usd == pytest.approx(20.0)


def test_estimate_usd_unknown_model_returns_zero():
    assert cost.estimate_usd("madeup/no-such-model", 1000, 500) == 0.0


def test_record_with_no_usage_is_safe():
    cost.record("openai/gpt-4o", None)
    cost.record("openai/gpt-4o", {})
    assert cost.session_stats().prompt_tokens == 0


def test_user_price_override_via_env(monkeypatch):
    monkeypatch.setattr(
        config, "MODEL_PRICES_JSON",
        json.dumps({"madeup/x": {"input_per_million": 100, "output_per_million": 200}}),
    )
    usd = cost.estimate_usd("madeup/x", 1_000_000, 1_000_000)
    assert usd == pytest.approx(300.0)


def test_render_summary_includes_session_and_turn():
    cost.record("openai/gpt-4o", {"prompt_tokens": 100, "completion_tokens": 50})
    out = cost.render_summary()
    assert "this turn" in out and "this session" in out
    assert "openai/gpt-4o" in out  # by-model breakdown
    assert "100" in out


def test_reset_session_clears_everything():
    cost.record("openai/gpt-4o", {"prompt_tokens": 100, "completion_tokens": 50})
    cost.reset_session()
    assert cost.session_stats().prompt_tokens == 0
    assert cost.turn_stats().prompt_tokens == 0
    assert cost.by_model() == {}
