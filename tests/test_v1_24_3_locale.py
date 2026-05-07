"""Tests for v1.24.3 emoji-safe terminal rendering.

Sam ran into "Ã°ÂÂÂ" mojibake on his Ubuntu deploy under tmux —
UTF-8 emoji bytes interpreted as Latin-1 (sometimes doubly encoded
along the SSH/tmux chain). v1.24.3 detects bad locales and falls
back to ASCII glyphs.
"""
from __future__ import annotations

import os

import pytest


# ---------- detection heuristic ----------


def test_terminal_safe_with_utf8_locale(monkeypatch):
    """LANG=en_US.UTF-8 + UTF-8 stdout → safe."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    # stdout encoding can vary in test runners; force-set the env
    # heuristic and verify based on known good locales when possible.
    from janus import branding
    if "utf" in (getattr(__import__("sys").stdout, "encoding", "") or "").lower():
        assert branding._terminal_is_emoji_safe() is True


def test_terminal_unsafe_with_c_locale(monkeypatch):
    """LANG=C → unsafe regardless of stdout."""
    monkeypatch.setenv("LANG", "C")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    from janus import branding
    assert branding._terminal_is_emoji_safe() is False


def test_terminal_unsafe_with_posix_locale(monkeypatch):
    monkeypatch.setenv("LANG", "POSIX")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    from janus import branding
    assert branding._terminal_is_emoji_safe() is False


def test_terminal_unsafe_with_ansi_x3_locale(monkeypatch):
    """ANSI_X3.4-1968 is the legacy POSIX 7-bit-ASCII locale name."""
    monkeypatch.setenv("LANG", "ANSI_X3.4-1968")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    from janus import branding
    assert branding._terminal_is_emoji_safe() is False


def test_lc_all_overrides_lang(monkeypatch):
    """LC_ALL takes priority — POSIX rule."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.delenv("LC_CTYPE", raising=False)
    from janus import branding
    assert branding._terminal_is_emoji_safe() is False


# ---------- env-var override ----------


def test_janus_no_emoji_env_force_disable(monkeypatch):
    """JANUS_NO_EMOJI=1 disables emoji even on a healthy terminal."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    assert branding._emoji_disabled() is True


def test_janus_no_emoji_env_force_enable(monkeypatch):
    """JANUS_NO_EMOJI=0 enables emoji even on an unsafe terminal."""
    monkeypatch.setenv("LANG", "C")
    monkeypatch.setenv("JANUS_NO_EMOJI", "0")
    from janus import branding
    assert branding._emoji_disabled() is False


def test_janus_no_emoji_unset_uses_auto_detect(monkeypatch):
    monkeypatch.setenv("LANG", "C")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    from janus import branding
    assert branding._emoji_disabled() is True


# ---------- glyph() helper ----------


def test_glyph_returns_emoji_when_safe(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("JANUS_NO_EMOJI", "0")
    from janus import branding
    assert branding.glyph("🎯", "->") == "🎯"


def test_glyph_returns_fallback_when_unsafe(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    assert branding.glyph("🎯", "->") == "->"


# ---------- emoji_safe_text() filter ----------


def test_emoji_safe_text_strips_4_byte_emoji_when_disabled(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    out = branding.emoji_safe_text("hello 🎯 world 💡")
    # 4-byte emojis dropped.
    assert "🎯" not in out
    assert "💡" not in out
    # ASCII text intact.
    assert "hello" in out
    assert "world" in out


def test_emoji_safe_text_preserves_bmp_arrows(monkeypatch):
    """Common BMP arrows (→ ← ↑ ↓ ✓ ✗ ●) stay because they're safe
    on most UTF-8 terminals and Janus uses them everywhere."""
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    text = "→ tool_call ✓ ok ✗ fail ● mark"
    out = branding.emoji_safe_text(text)
    assert "→" in out
    assert "✓" in out
    assert "✗" in out
    assert "●" in out


def test_emoji_safe_text_noop_when_enabled(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "0")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    from janus import branding
    text = "🎯 hello 💡"
    assert branding.emoji_safe_text(text) == text


def test_emoji_safe_text_handles_empty_string():
    from janus import branding
    assert branding.emoji_safe_text("") == ""


# ---------- integration: chat output sanitization ----------


def test_cli_rich_streaming_uses_emoji_safe(monkeypatch):
    """Source-level pin: stream_chunk handler in cli_rich.py routes
    text through branding.emoji_safe_text before writing."""
    pytest.importorskip("rich")
    import inspect
    from janus import cli_rich
    src = inspect.getsource(cli_rich._render_step_factory)
    assert "emoji_safe_text" in src, (
        "cli_rich stream chunks should be sanitized for unsafe locales"
    )


def test_cli_basic_rendered_output_uses_emoji_safe():
    import inspect
    from janus import cli
    src = inspect.getsource(cli)
    assert "emoji_safe_text" in src, (
        "cli.py rendered output should be sanitized for unsafe locales"
    )
