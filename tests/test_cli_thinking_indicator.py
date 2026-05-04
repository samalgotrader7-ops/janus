"""Tests for v1.5.1 phase 2: CLI thinking indicator.

Bug: After typing a prompt, the user saw nothing for ~30s while Janus
was running tool calls. They thought it had hung. The fix is a
"⚡ thinking…" line printed BEFORE the first model call.

These tests verify the indicator is emitted in both cli_rich and the
basic cli, and that it appears as a string literal in the source so a
careless refactor doesn't drop it.
"""
from __future__ import annotations
import inspect

from janus import cli, cli_rich


# ---------- cli_rich (rich TUI) ----------


def test_cli_rich_emits_thinking_indicator_before_executor_call():
    """The thinking indicator must be emitted between approver setup and
    the actual executor.chat() invocation so the user sees activity
    during the tool-call gather phase."""
    src = inspect.getsource(cli_rich)
    # Indicator should be printed somewhere in the chat flow
    assert "⚡ thinking" in src
    # The CALL site (not earlier docstring/comment mentions) is what we
    # care about. Find the first `output, trace = executor.chat(` —
    # that's the actual invocation.
    call_marker = "output, trace = executor.chat("
    chat_idx = src.find(call_marker)
    assert chat_idx >= 0, "could not find the executor.chat() call site"
    indicator_idx = src.rfind("⚡ thinking", 0, chat_idx)
    assert indicator_idx >= 0, (
        "thinking indicator must appear before the executor.chat() call site"
    )
    # And the indicator should be CLOSE to the call (within ~500 chars
    # — i.e., immediately preceding setup, not way earlier).
    assert chat_idx - indicator_idx < 500, (
        "indicator should be in the same setup block as the call, "
        "not buried elsewhere in the file"
    )


def test_cli_rich_indicator_uses_dim_styling():
    """Should be subtle — not louder than the user's own text."""
    src = inspect.getsource(cli_rich)
    # Look for "dim" near the thinking line
    line_with_thinking = next(
        l for l in src.splitlines() if "⚡ thinking" in l
    )
    assert "dim" in line_with_thinking.lower()


# ---------- basic cli ----------


def test_cli_basic_emits_thinking_indicator():
    src = inspect.getsource(cli)
    assert "⚡ thinking" in src


def test_cli_basic_indicator_uses_dim_color():
    """C.DIM (or similar muted color) should wrap the thinking text."""
    src = inspect.getsource(cli)
    line_with_thinking = next(
        l for l in src.splitlines() if "⚡ thinking" in l
    )
    assert "DIM" in line_with_thinking or "dim" in line_with_thinking.lower()


def test_cli_basic_indicator_before_executor_call():
    src = inspect.getsource(cli)
    call_marker = "output, trace = executor.chat("
    chat_idx = src.find(call_marker)
    assert chat_idx >= 0
    indicator_idx = src.rfind("⚡ thinking", 0, chat_idx)
    assert indicator_idx >= 0
    assert chat_idx - indicator_idx < 500


# ---------- Glyph consistency ----------


def test_cli_thinking_uses_same_glyph_as_indicator_kinds():
    """The CLI thinking line should use ⚡, the same glyph
    INDICATOR_GLYPHS['thinking'] uses (consistency across surfaces)."""
    from janus.gateways._common import INDICATOR_GLYPHS
    assert INDICATOR_GLYPHS["thinking"] == "⚡"
    cli_src = inspect.getsource(cli)
    cli_rich_src = inspect.getsource(cli_rich)
    assert "⚡" in cli_src
    assert "⚡" in cli_rich_src
