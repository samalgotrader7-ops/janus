"""Tests for v1.5 phase 8: Telegram completion ping for background swarms."""
from __future__ import annotations

import pytest

from janus import __main__ as main_mod
from janus import config


@pytest.fixture
def captured_post(monkeypatch):
    """Replace requests.post so we can verify the Telegram API call
    without hitting the network."""
    calls: list = []

    def _post(url, **kw):
        calls.append({"url": url, "kw": kw})

        class R:
            status_code = 200
            def raise_for_status(self_inner): pass
            def json(self_inner): return {"ok": True}
        return R()

    import requests
    monkeypatch.setattr(requests, "post", _post)
    return calls


@pytest.fixture
def home_channel_set(tmp_path, monkeypatch):
    """Configure a home channel + bot token so the ping fires."""
    monkeypatch.setattr(config, "HOME", tmp_path)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    # Write a home_channels.json
    home_file = tmp_path / "home_channels.json"
    home_file.write_text('{"telegram": "12345"}', encoding="utf-8")
    yield


# ---------- _ping_home_channel ----------


def test_ping_no_token_no_post(monkeypatch, captured_post, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "")
    main_mod._ping_home_channel("run-1", "demo", n_phases=2)
    assert captured_post == []


def test_ping_no_home_channel_no_post(monkeypatch, captured_post, tmp_path):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(config, "HOME", tmp_path)
    # No home_channels.json at all
    main_mod._ping_home_channel("run-1", "demo", n_phases=2)
    assert captured_post == []


def test_ping_success_sends_completion_message(
    monkeypatch, captured_post, home_channel_set,
):
    main_mod._ping_home_channel("swarm-r1", "data-scrape", n_phases=3)
    assert len(captured_post) == 1
    call = captured_post[0]
    assert "api.telegram.org/bottest-token/sendMessage" in call["url"]
    body = call["kw"]["json"]
    assert body["chat_id"] == "12345"
    assert "complete" in body["text"]
    assert "data-scrape" in body["text"]
    assert "swarm-r1" in body["text"]
    assert "phases: 3" in body["text"]


def test_ping_failure_sends_failure_message(
    monkeypatch, captured_post, home_channel_set,
):
    main_mod._ping_home_channel(
        "swarm-r1", "data-scrape", error="budget_exceeded: $5 > $1",
    )
    body = captured_post[0]["kw"]["json"]
    assert "FAILED" in body["text"]
    assert "budget_exceeded" in body["text"]


def test_ping_swallows_post_exception(
    monkeypatch, home_channel_set,
):
    """P8: notification failure never propagates."""
    import requests

    def boom(url, **kw):
        raise requests.exceptions.ConnectionError("network down")

    monkeypatch.setattr(requests, "post", boom)
    # Should NOT raise
    main_mod._ping_home_channel("swarm-r1", "demo", n_phases=1)


def test_ping_swallows_get_home_exception(monkeypatch, home_channel_set):
    """If get_home raises (corrupt home_channels.json, race condition),
    ping no-ops rather than crashing the swarm."""
    from janus.gateways import _common as gw

    def boom(*a, **kw):
        raise ValueError("corrupt config")

    monkeypatch.setattr(gw, "get_home", boom)
    # Should NOT raise.
    main_mod._ping_home_channel("r", "s", n_phases=0)
