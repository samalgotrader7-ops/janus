"""Tests for v1.35.1 — prompt-cache provider detection (Phase 9.3)."""

from __future__ import annotations

import pytest

from janus import llm


@pytest.mark.parametrize("base,expected", [
    ("https://openrouter.ai/api/v1", "openrouter"),
    ("https://api.anthropic.com/v1", "anthropic"),
    ("https://bedrock-runtime.us-east-1.amazonaws.com", "bedrock"),
    ("https://api.openai.com/v1", "openai"),
    ("https://us-central1-aiplatform.googleapis.com", "vertex"),
    ("https://vertex.example/v1", "vertex"),
    ("http://localhost:11434/v1", "ollama"),
    ("http://127.0.0.1:8080/v1", "ollama"),
    ("https://some-unknown-host.example/v1", "unknown"),
    ("", "unknown"),
])
def test_detect_provider(base, expected):
    assert llm.detect_provider(base) == expected


@pytest.mark.parametrize("provider,supported", [
    ("openrouter", True),
    ("anthropic", True),
    ("bedrock", True),
    ("openai", False),
    ("vertex", False),
    ("ollama", False),
    ("unknown", False),
])
def test_cache_supported(provider, supported):
    assert llm.cache_supported(provider) == supported


def test_cache_beneficiaries_immutable():
    """frozenset prevents accidental mutation."""
    with pytest.raises((AttributeError, TypeError)):
        llm.CACHE_BENEFICIARIES.add("openai")  # type: ignore


def test_detect_provider_uses_config_when_none(monkeypatch):
    """When called with no arg, falls back to config.API_BASE."""
    from janus import config
    monkeypatch.setattr(config, "API_BASE", "https://api.anthropic.com/v1")
    monkeypatch.setattr(llm.config, "API_BASE", "https://api.anthropic.com/v1")
    assert llm.detect_provider() == "anthropic"


def test_apply_cache_markers_still_works_unchanged(monkeypatch):
    """Regression: existing apply_cache_markers behavior preserved."""
    monkeypatch.setattr(llm.config, "PROMPT_CACHE_MARKERS", False)
    msgs = [{"role": "system", "content": "x"}]
    out = llm.apply_cache_markers(msgs)
    # No-op when flag off
    assert out == msgs


def test_version_bumped_to_1_35_1_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 1)
