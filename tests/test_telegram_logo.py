"""Tests for the Telegram bot's logo wiring.

We don't spawn a real bot. We verify the helper that builds the logo
block produces well-formed Markdown that renders the bifurcation arrows
inside a code fence (so Telegram clients show them in monospace).
"""
from __future__ import annotations

import pytest

# python-telegram-bot is optional. If not installed, all telegram tests skip.
pytest.importorskip("telegram", reason="python-telegram-bot not installed")

from janus.gateways import telegram as tg
from janus import branding


def test_logo_block_contains_all_three_arrows():
    body = tg._logo_block()
    for line in branding.LOGO_LINES:
        assert line in body


def test_logo_block_uses_markdown_code_fence():
    body = tg._logo_block()
    # Telegram's Markdown V1 monospace = triple-backtick code block.
    assert body.count("```") == 2
    # Logo lines live INSIDE the fence so monospace renders the arrows.
    pre, fence_open = body.split("```\n", 1)
    inside, _ = fence_open.split("\n```", 1)
    for line in branding.LOGO_LINES:
        assert line in inside


def test_logo_block_includes_version_and_tagline():
    body = tg._logo_block()
    assert f"v{branding.VERSION}" in body
    assert branding.TAGLINE in body
