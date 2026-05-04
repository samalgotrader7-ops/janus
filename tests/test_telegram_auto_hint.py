"""Tests for v1.5.2 phase 3: /mode auto hint in Telegram greeting.

Bug context: J9/J10 showed Sam's Telegram chat with default mode —
every fs_write / shell call triggered the 4-button approval keyboard
which Sam didn't tap (or didn't see), and the approver timed out
returning False (X marks). Result: long sequences of refused tool
calls before the bot gave up.

Fix: when a brand-new chat starts in default mode, the first-greeting
message includes a tip about /mode auto. Auto mode allows-with-
risk-analysis (rm -rf /, /etc/, SSRF still block) but doesn't
interrupt for every write/exec.
"""
from __future__ import annotations
import inspect

from janus.gateways import telegram


def test_telegram_greeting_includes_auto_hint_in_default_mode_source():
    """Source-level pin: the on_text greeting flow includes a Markdown
    hint about /mode auto when sess.mode_state.current == DEFAULT."""
    src = inspect.getsource(telegram.on_text)
    # The hint
    assert "/mode auto" in src
    # The condition: only when in default mode
    assert "permissions.DEFAULT" in src or "DEFAULT" in src
    # Mentions auto mode's safety net so users know it's not bypass
    safety_keywords = ("rm -rf", "/etc/", "SSRF", "block")
    matches = sum(1 for k in safety_keywords if k in src)
    assert matches >= 2, (
        "the hint should mention what auto mode still BLOCKS so users "
        "don't think it's an unsafe carte blanche"
    )


def test_greeting_message_uses_markdown_parse_mode():
    """The greeting includes Markdown formatting (backtick code blocks
    around /mode auto), so reply_text needs parse_mode='Markdown'."""
    src = inspect.getsource(telegram.on_text)
    # Find the greeting reply call
    greeting_block_start = src.find("greeting = gw.greeting")
    assert greeting_block_start >= 0
    block = src[greeting_block_start:greeting_block_start + 800]
    assert 'parse_mode="Markdown"' in block or "parse_mode='Markdown'" in block


def test_greeting_hint_is_only_for_default_mode():
    """In auto / acceptEdits / plan / bypassPermissions modes, the hint
    shouldn't fire (those users have already chosen a posture)."""
    src = inspect.getsource(telegram.on_text)
    # The runtime check — look for the literal mode-comparison line.
    # The string `mode_state.current == permissions.DEFAULT` appears in
    # the code (inside an `if`); verify it's ABOVE the hint append.
    guard = "sess.mode_state.current == permissions.DEFAULT"
    guard_idx = src.find(guard)
    assert guard_idx >= 0, (
        "Expected an `if sess.mode_state.current == permissions.DEFAULT:` "
        "guard around the hint"
    )
    # The hint string `Tip: try \`/mode auto\`` should appear after the guard.
    hint_idx = src.find("Tip: try", guard_idx)
    assert hint_idx > guard_idx, (
        "The hint must be inside the DEFAULT-mode guard"
    )
