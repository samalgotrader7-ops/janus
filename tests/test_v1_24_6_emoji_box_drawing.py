"""Tests for v1.24.6 emoji-safe terminal rendering — extended ranges
+ tighter tmux heuristic.

v1.24.3 covered 4-byte emoji but missed two real-world cases on Sam's
2026-05-07 Ubuntu deploy:

  1. Box-drawing chars (U+2500-U+257F) — when the model emits a
     markdown ASCII-art frame using ╔ ═ ║ etc., tmux mangled those
     into ``Ã¢ÂÂ%`` mojibake just like the emoji.

  2. The locale auto-detect returned ``True`` (safe) on Sam's box
     because LANG=en_US.UTF-8 was set + stdout encoding was UTF-8 —
     even though tmux's TERM was 'screen' / 'screen-256color' which
     drops anything past 7-bit.

v1.24.6 strips box-drawing + the geometric-shapes block, replaces
common box characters with ASCII so tables don't disintegrate, and
treats `TMUX` set + a non-utf TERM as unsafe.
"""
from __future__ import annotations


# ---------- box-drawing strip ----------


def test_box_drawing_replaced_when_disabled(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    # Mix of common box-drawing chars seen in Sam's screenshot.
    text = "╔══════╗\n║ hi  ║\n╚══════╝"
    out = branding.emoji_safe_text(text)
    # No mangled glyphs left.
    for ch in "╔═║╚╗╝":
        assert ch not in out, f"box char {ch!r} should be replaced"
    # Visual structure preserved via ASCII substitutes.
    assert "+" in out  # corners and unmapped junctions
    assert "|" in out  # verticals
    # Three lines preserved (newlines are ASCII, untouched).
    assert out.count("\n") == 2


def test_box_drawing_horizontal_collapses_to_dashes(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    out = branding.emoji_safe_text("─" * 5)
    assert out == "-" * 5


def test_box_drawing_vertical_collapses_to_pipes(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    out = branding.emoji_safe_text("│" * 3)
    assert out == "|" * 3


def test_geometric_shapes_dropped_when_disabled(monkeypatch):
    """U+25A0-U+25FF outside our allowlist (■ □ ▲ ▼ ▓ ▒ ░ etc.) should
    drop. ● ▸ ◂ stay because the agent uses them for status markers."""
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    text = "● keep ▓ drop ░ drop ▸ keep ◂ keep ■ drop"
    out = branding.emoji_safe_text(text)
    assert "●" in out
    assert "▸" in out
    assert "◂" in out
    assert "▓" not in out
    assert "░" not in out
    assert "■" not in out


def test_box_drawing_noop_when_enabled(monkeypatch):
    monkeypatch.setenv("JANUS_NO_EMOJI", "0")
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    from janus import branding
    text = "╔══╗\n║ X ║\n╚══╝"
    assert branding.emoji_safe_text(text) == text


def test_logo_arrows_still_preserved_with_extension(monkeypatch):
    """Regression: v1.24.3's allowlist (→ ← ↑ ↓ ✓ ✗ ●) must keep
    working under the new box-drawing pass — Janus's own logo and
    tool-result markers depend on them."""
    monkeypatch.setenv("JANUS_NO_EMOJI", "1")
    from janus import branding
    text = "→ tool ✓ ok ✗ fail ● mark"
    out = branding.emoji_safe_text(text)
    assert "→" in out
    assert "✓" in out
    assert "✗" in out
    assert "●" in out


# ---------- tmux + screen TERM heuristic ----------


def test_tmux_with_screen_term_treated_unsafe(monkeypatch):
    """TMUX env set + TERM=screen → tmux's default-terminal isn't
    UTF-8 capable; auto-detect should bail to unsafe."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "screen")
    from janus import branding
    assert branding._terminal_is_emoji_safe() is False


def test_tmux_with_screen_256color_treated_unsafe(monkeypatch):
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "screen-256color")
    from janus import branding
    assert branding._terminal_is_emoji_safe() is False


def test_tmux_with_tmux_256color_treated_safe(monkeypatch):
    """The recommended fix is `set -g default-terminal "tmux-256color"`.
    With that set, the heuristic should NOT bail."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "tmux-256color")
    import sys
    from janus import branding
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in enc:
        assert branding._terminal_is_emoji_safe() is True


def test_tmux_with_xterm_utf8_term_treated_safe(monkeypatch):
    """xterm-256color with .UTF-8 hint or just 'utf' substring counts
    as safe even under TMUX."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "xterm-utf8")
    import sys
    from janus import branding
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in enc:
        assert branding._terminal_is_emoji_safe() is True


def test_no_tmux_screen_term_still_safe(monkeypatch):
    """Outside tmux, TERM=screen alone shouldn't trigger the new
    bail — that's a screen(1) session, different ballgame and rarer."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    monkeypatch.delenv("LC_CTYPE", raising=False)
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TERM", "screen")
    import sys
    from janus import branding
    enc = (getattr(sys.stdout, "encoding", "") or "").lower()
    if "utf" in enc:
        assert branding._terminal_is_emoji_safe() is True


def test_janus_no_emoji_zero_overrides_tmux_heuristic(monkeypatch):
    """User can always force emoji on with JANUS_NO_EMOJI=0 even
    inside tmux+screen — escape hatch."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "screen")
    monkeypatch.setenv("JANUS_NO_EMOJI", "0")
    from janus import branding
    assert branding._emoji_disabled() is False


# ---------- end-to-end: Sam's exact mojibake line ----------


def test_sams_2026_05_07_smiley_stripped(monkeypatch):
    """Reproduce: model streams 'Alright Sam, let me explain it here
    then! 🙂' under tmux+screen → smiley should drop, ASCII intact."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "screen")
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    from janus import branding
    line = "Alright Sam, let me just explain it here then! 🙂"
    out = branding.emoji_safe_text(line)
    assert "🙂" not in out
    assert "Alright Sam" in out
    assert out.endswith("!") or out.endswith("! ")


def test_sams_2026_05_07_ascii_art_table_survives(monkeypatch):
    """Reproduce: model output box-drawing tables, tmux mangled them.
    With v1.24.6 strip, the structure survives via ASCII substitutes."""
    monkeypatch.setenv("LANG", "en_US.UTF-8")
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,42,0")
    monkeypatch.setenv("TERM", "screen")
    monkeypatch.delenv("JANUS_NO_EMOJI", raising=False)
    from janus import branding
    table = "╔════════════╗\n║ Phase 1    ║\n╚════════════╝"
    out = branding.emoji_safe_text(table)
    for ch in "╔═║╚╗╝":
        assert ch not in out
    # Three rows preserved.
    assert out.count("\n") == 2
