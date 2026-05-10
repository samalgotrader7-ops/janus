"""Tests for v1.35.0 — multi-model routing per purpose (Phase 9.4)."""

from __future__ import annotations

import importlib

import pytest


def _reload(monkeypatch):
    from janus import config
    importlib.reload(config)
    return config


def test_chat_purpose_returns_main_model(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main-model")
    config = _reload(monkeypatch)
    assert config.model_for_purpose("chat") == "main-model"


def test_memory_falls_back_to_main_when_unset(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main-model")
    monkeypatch.delenv("JANUS_MEMORY_MODEL", raising=False)
    config = _reload(monkeypatch)
    assert config.model_for_purpose("memory") == "main-model"


def test_memory_uses_override_when_set(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main-model")
    monkeypatch.setenv("JANUS_MEMORY_MODEL", "cheap-memory-model")
    config = _reload(monkeypatch)
    assert config.model_for_purpose("memory") == "cheap-memory-model"


def test_verify_uses_override(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main")
    monkeypatch.setenv("JANUS_VERIFY_MODEL", "verify-model")
    config = _reload(monkeypatch)
    assert config.model_for_purpose("verify") == "verify-model"


def test_subagent_uses_override(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main")
    monkeypatch.setenv("JANUS_SUBAGENT_MODEL", "subagent-model")
    config = _reload(monkeypatch)
    assert config.model_for_purpose("subagent") == "subagent-model"


def test_title_uses_override(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main")
    monkeypatch.setenv("JANUS_TITLE_MODEL", "title-model")
    config = _reload(monkeypatch)
    assert config.model_for_purpose("title") == "title-model"


def test_unknown_purpose_falls_back_to_main(monkeypatch):
    monkeypatch.setenv("JANUS_MODEL", "main-model")
    config = _reload(monkeypatch)
    assert config.model_for_purpose("unknown-purpose") == "main-model"
    assert config.model_for_purpose("") == "main-model"


def test_per_purpose_independence(monkeypatch):
    """Setting JANUS_VERIFY_MODEL doesn't affect memory routing."""
    monkeypatch.setenv("JANUS_MODEL", "main")
    monkeypatch.setenv("JANUS_VERIFY_MODEL", "verify-only")
    monkeypatch.delenv("JANUS_MEMORY_MODEL", raising=False)
    config = _reload(monkeypatch)
    assert config.model_for_purpose("verify") == "verify-only"
    assert config.model_for_purpose("memory") == "main"


def test_version_bumped_to_1_35_0_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 35, 0)
