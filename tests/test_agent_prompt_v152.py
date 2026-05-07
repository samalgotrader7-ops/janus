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
    """The original 'do not paste content' directive must survive the split.
    v1.26.0: the gateway-send guidance lives as a bullet in section 2
    (Tool selection) — slice on the bullet header instead of legacy
    rule numbering."""
    s = executor.JANUS_CHAT_SYSTEM
    rule_start = s.find("File sends via gateway")
    assert rule_start >= 0, "expected the gateway-send bullet"
    # Slice forward to where the next section starts, or 1500 chars
    # whichever comes first — long enough to span the bullet body.
    end = s.find("# 3. Memory", rule_start)
    if end == -1:
        end = rule_start + 1500
    body = s[rule_start:end]
    # Both pasting-forbidden phrasings remain valid.
    assert "do NOT paste" in body or "do not paste" in body.lower()


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


def test_prompt_has_six_grouped_sections():
    """v1.26.0 replaced numbered rules (1-23) with 6 grouped sections.
    Pin the section count so future edits don't silently collapse them."""
    s = executor.JANUS_CHAT_SYSTEM
    for header in (
        "# 1. Tone",
        "# 2. Tool selection",
        "# 3. Memory",
        "# 4. Verification",
        "# 5. Mode",
        "# 6. Errors",
    ):
        assert header in s, f"missing section header: {header!r}"


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
