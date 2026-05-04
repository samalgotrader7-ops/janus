"""Tests for v1.5.2 phase 2: system prompt updates pinning the
fail-fast / concise-answer / gateway-vs-telegram-tool directives.

These pin the new behavior so future edits can't silently regress.
"""
from __future__ import annotations

from janus import executor


# ---------- Rule 4 split: gateway_send_file vs telegram_send_file ----------


def test_rule_4_disambiguates_gateway_send_file_and_telegram_send_file():
    s = executor.JANUS_CHAT_SYSTEM
    # Both tools mentioned
    assert "gateway_send_file" in s
    assert "telegram_send_file" in s
    # The disambiguation is by CONTEXT — must mention both
    assert "Inside the Telegram gateway chat" in s or "inside the telegram gateway" in s.lower()
    assert "CLI" in s or "cli" in s.lower()


def test_rule_4_says_telegram_send_file_takes_chat_id():
    s = executor.JANUS_CHAT_SYSTEM
    assert "telegram_send_file(path, chat_id)" in s or "telegram_send_file(path,chat_id)" in s


def test_rule_4_hints_at_session_recent_for_chat_id():
    s = executor.JANUS_CHAT_SYSTEM
    assert "session_recent" in s


def test_rule_4_still_forbids_pasting_content():
    """The original 'do not paste content' directive must survive the split."""
    s = executor.JANUS_CHAT_SYSTEM
    # Look in the rule 4 body
    rule4_start = s.find("4. **When the user asks to send a file")
    rule5_start = s.find("5. **")
    assert rule4_start >= 0 and rule5_start > rule4_start
    rule4_body = s[rule4_start:rule5_start]
    assert "do NOT paste" in rule4_body or "do not paste" in rule4_body.lower()


# ---------- Rule 8: tool fail → adapt fast ----------


def test_rule_8_says_adapt_fast_on_failure():
    s = executor.JANUS_CHAT_SYSTEM
    assert "ADAPT FAST" in s or "adapt fast" in s.lower()


def test_rule_8_caps_alternative_attempts():
    s = executor.JANUS_CHAT_SYSTEM
    assert "ONE alternative" in s or "one alternative" in s.lower()


def test_rule_8_says_one_sentence_failure_message():
    s = executor.JANUS_CHAT_SYSTEM
    assert "ONE sentence" in s or "one sentence" in s.lower()


def test_rule_8_forbids_paragraphs_about_architecture():
    s = executor.JANUS_CHAT_SYSTEM
    # Must explicitly forbid "explaining the architecture" type behavior
    assert "paragraphs" in s.lower()
    assert "architecture" in s.lower() or "config" in s.lower()


# ---------- Rule 9: answer directly ----------


def test_rule_9_says_answer_directly():
    s = executor.JANUS_CHAT_SYSTEM
    assert "DIRECTLY" in s or "directly" in s.lower()


def test_rule_9_uses_concrete_example():
    """The directive should include a contrastive example so the model
    can pattern-match: "where is the file" → path, not narrative."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "where is the file" in s.lower()


def test_rule_9_says_trim_non_answers():
    s = executor.JANUS_CHAT_SYSTEM
    assert "trim" in s.lower() or "isn't the answer" in s.lower()


# ---------- Rule count check ----------


def test_prompt_has_at_least_9_numbered_rules():
    """Pin the new total — adding new rules later should bump this; rules
    should never silently shrink."""
    s = executor.JANUS_CHAT_SYSTEM
    # Look for "9. **" — rule 9 marker
    assert "9. **" in s


# ---------- Earlier directives preserved ----------


def test_earlier_directives_still_present():
    """Rules from v1.5.1 phase 1 must survive — no regression."""
    s = executor.JANUS_CHAT_SYSTEM
    # Rule 1: write file → fs_write
    assert "fs_write" in s
    # Rule 5: uploaded image
    assert "[user uploaded image" in s
    # Rule 6: ≤2 sentences summary
    assert "<2 sentences" in s or "1-2 sentences" in s
    # Rule 7: default to ACT
    assert "default to ACT" in s
    # Agent-not-chatbot
    assert "AGENT" in s
