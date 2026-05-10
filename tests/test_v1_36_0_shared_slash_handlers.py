"""Tests for v1.36.0 — slash dispatcher migration proof (Phase 8.1)."""

from __future__ import annotations

import pytest

from janus import slash_dispatch as sd


@pytest.fixture
def reg():
    r = sd.SlashRegistry()
    sd.register_shared_handlers(r)
    return r


def _ctx(state=None):
    return sd.SlashContext(
        surface="test",
        state=state or {},
        console=None,
        print_fn=lambda s: None,
    )


def test_version_handler(reg):
    handled, out = reg.dispatch("/version", _ctx())
    assert handled is True
    assert "janus" in out.lower()
    # Tagline appears in output
    assert "agent" in out.lower() or "plain-text" in out.lower()


def test_cwd_handler(reg):
    handled, out = reg.dispatch("/cwd", _ctx())
    assert handled is True
    assert isinstance(out, str)
    assert len(out) > 0


def test_home_handler(reg):
    handled, out = reg.dispatch("/home", _ctx())
    assert handled is True
    # Should reference the .janus path
    assert ".janus" in out or "janus" in out


def test_uptime_no_start_ts(reg):
    handled, out = reg.dispatch("/uptime", _ctx(state={}))
    assert handled is True
    assert "not tracked" in out


def test_uptime_with_start_ts(reg):
    import time
    state = {"_session_start_ts": time.time() - 65}  # 65s ago
    handled, out = reg.dispatch("/uptime", _ctx(state=state))
    assert handled is True
    assert "1m" in out


def test_provider_handler(reg):
    handled, out = reg.dispatch("/provider", _ctx())
    assert handled is True
    assert "provider:" in out
    assert "base:" in out


def test_all_new_handlers_registered(reg):
    """Pin: every Phase 8.1 handler is present after register_shared."""
    for cmd in ("/version", "/cwd", "/home", "/uptime", "/provider", "/grants"):
        assert reg.has(cmd), f"missing handler: {cmd}"


def test_unknown_command_returns_not_handled(reg):
    handled, out = reg.dispatch("/totallyfake", _ctx())
    assert handled is False


def test_version_bumped_to_1_36_0_or_later():
    from janus import branding
    parts = tuple(int(x) for x in branding.VERSION.split("."))
    assert parts >= (1, 36, 0)
