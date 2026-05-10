"""Tests for v1.34.8 — cross-gateway interview state (Phase 8.2)."""

from __future__ import annotations

import importlib

import pytest

from janus import interviews


def test_default_per_gateway_filenames(monkeypatch):
    monkeypatch.delenv("JANUS_INTERVIEW_CROSS_GATEWAY", raising=False)
    p_tg = interviews.state_path("telegram", "alice")
    p_web = interviews.state_path("web", "alice")
    assert p_tg.name != p_web.name
    assert "telegram" in p_tg.name
    assert "web" in p_web.name


def test_cross_gateway_uses_shared_filename(monkeypatch):
    monkeypatch.setenv("JANUS_INTERVIEW_CROSS_GATEWAY", "1")
    p_tg = interviews.state_path("telegram", "alice")
    p_web = interviews.state_path("web", "alice")
    p_cli = interviews.state_path("cli_rich", "alice")
    # All three resolve to the same file
    assert p_tg.name == p_web.name == p_cli.name
    assert "shared" in p_tg.name


def test_cross_gateway_still_separates_users(monkeypatch):
    """Same user across gateways → same file. Different users → different files."""
    monkeypatch.setenv("JANUS_INTERVIEW_CROSS_GATEWAY", "1")
    p_alice = interviews.state_path("telegram", "alice")
    p_bob = interviews.state_path("telegram", "bob")
    assert p_alice.name != p_bob.name


def test_cross_gateway_flag_truthy_strings(monkeypatch):
    for val in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("JANUS_INTERVIEW_CROSS_GATEWAY", val)
        p = interviews.state_path("telegram", "x")
        assert "shared" in p.name, f"failed for value {val!r}"


def test_cross_gateway_flag_falsy_strings(monkeypatch):
    for val in ("0", "false", "no", ""):
        monkeypatch.setenv("JANUS_INTERVIEW_CROSS_GATEWAY", val)
        p = interviews.state_path("telegram", "x")
        assert "telegram" in p.name


def test_load_save_round_trip_cross_gateway(monkeypatch, tmp_path):
    """End-to-end: save state from telegram, load from web,
    should see the same data."""
    monkeypatch.setenv("JANUS_INTERVIEW_CROSS_GATEWAY", "1")
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(interviews.config, "HOME", tmp_path)
    # Ensure state dir is created fresh per test
    state = interviews.load_state("telegram", "alice")
    state.mode = "drip"
    interviews.save_state(state)
    loaded = interviews.load_state("web", "alice")
    assert loaded.mode == "drip"


def test_per_gateway_isolated_when_flag_off(monkeypatch, tmp_path):
    monkeypatch.delenv("JANUS_INTERVIEW_CROSS_GATEWAY", raising=False)
    from janus import config
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(interviews.config, "HOME", tmp_path)
    state = interviews.load_state("telegram", "alice")
    state.mode = "drip"
    interviews.save_state(state)
    loaded = interviews.load_state("web", "alice")
    assert loaded.mode == "idle"


def test_version_bumped_to_1_34_8_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 34, 8)
