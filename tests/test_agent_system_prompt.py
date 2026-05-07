"""Tests for v1.5.1 phase 1: agent-vs-chatbot system prompt rewrite.

The model was acting as a chatbot ("explain what to do") instead of an
agent ("do the work"). The system prompt now contains explicit directives
to bias toward action — these tests pin the directives so future edits
don't silently water them down.
"""
from __future__ import annotations

from janus import executor


# ---------- Headline framing ----------


def test_system_prompt_says_agent_not_chatbot():
    """The opening line declares the agent identity."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "AGENT" in s
    assert "not a chatbot" in s.lower() or "not a chat" in s.lower()


def test_system_prompt_distinguishes_agent_from_chatbot():
    """Explains the agent ↔ chatbot distinction."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "Agents DO" in s or "agents do" in s.lower()
    assert "EXPLAIN" in s or "describe" in s.lower()


# ---------- Action directives ----------


def test_system_prompt_says_write_a_file_means_fs_write():
    """The single most important directive given Sam's bug report."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "fs_write" in s
    # Must explicitly say don't paste content
    assert "Do NOT paste" in s or "do not paste" in s.lower()


def test_system_prompt_says_default_to_file_for_reports():
    s = executor.JANUS_CHAT_SYSTEM
    assert "comparison" in s.lower() or "report" in s.lower()
    assert "FILE" in s or "file" in s


def test_system_prompt_lists_explicit_inline_keywords():
    """Tells the model when inline IS appropriate so it doesn't over-correct."""
    s = executor.JANUS_CHAT_SYSTEM
    # The carve-out keywords should appear
    inline_keywords = ["tell me", "show me", "paste it"]
    matches = sum(1 for k in inline_keywords if k in s.lower())
    assert matches >= 2  # at least 2 of the 3


def test_system_prompt_forbids_let_me_preamble():
    """No 'Let me…' / 'I'll…' narration before tool calls."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "Let me" in s  # must be MENTIONED to forbid
    assert "I'll" in s
    # Should be in a forbidding context. v1.26.0: the rule lives in
    # section 1 (Tone) of the rewritten prompt, so slice that section
    # rather than the legacy "RULES"/"WHEN CHAT" markers.
    sec1 = s[s.find("# 1. Tone"):s.find("# 2. Tool selection")]
    assert "preface" in sec1.lower() or "do not" in sec1.lower() \
        or "don't preface" in sec1.lower()


# ---------- Gateway / file directives ----------


def test_system_prompt_says_send_file_to_telegram_uses_gateway_tool():
    """The fix for J4: the model must know to use gateway_send_file when
    asked to "send me the file to telegram"."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "gateway_send_file" in s
    assert "telegram" in s.lower()


def test_system_prompt_says_uploaded_image_path_is_the_image():
    """Fix for J5: when telegram dumps `[user uploaded image at /path]`
    into the conversation, the model must recognize the path AS the image,
    not respond "I don't see any image"."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "uploaded" in s.lower()
    assert "image_describe" in s or "fs_read" in s
    # Negative directive
    assert "don't see" in s.lower() or "do not say" in s.lower()


# ---------- Brevity directives ----------


def test_system_prompt_caps_summary_length():
    """When task complete, summary should be brief."""
    s = executor.JANUS_CHAT_SYSTEM
    # Looking for a length cap directive
    assert "<2 sentences" in s or "1-2 sentences" in s or "one sentence" in s.lower()


def test_system_prompt_forbids_listing_every_tool_call():
    s = executor.JANUS_CHAT_SYSTEM
    assert "tool call" in s.lower()
    # Should be in a forbidding context
    assert "Don't" in s or "Do NOT" in s or "do not" in s.lower()


# ---------- Default-to-act bias ----------


def test_system_prompt_says_default_to_act_when_uncertain():
    s = executor.JANUS_CHAT_SYSTEM
    assert "default to ACT" in s or "default to act" in s.lower()


def test_system_prompt_explains_permission_mode_doesnt_need_user_check():
    """The model should not pre-ask the user before tool calls; the mode
    machinery does that."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "permission mode" in s.lower()
    assert "do not need to ask" in s.lower() or "don't need to ask" in s.lower()


# ---------- Mode awareness ----------


def test_system_prompt_lists_all_5_modes():
    """All five v1.5 modes documented for the model's awareness."""
    s = executor.JANUS_CHAT_SYSTEM
    for mode in ("default", "acceptEdits", "plan", "auto", "bypassPermissions"):
        assert mode in s


# ---------- Configuration surface ----------


def test_system_prompt_lists_v15_paths():
    s = executor.JANUS_CHAT_SYSTEM
    # Updated for v1.5 multi-category memory + swarms
    assert "memory/" in s
    assert "swarms/" in s


def test_system_prompt_does_not_invent_paths():
    """Anti-hallucination directive preserved from earlier versions."""
    s = executor.JANUS_CHAT_SYSTEM
    assert "invent" in s.lower() or "fabricat" in s.lower()


# ---------- Build / round-trip ----------


def test_build_chat_system_includes_agent_identity(tmp_path):
    out = executor._build_chat_system(
        workspace=str(tmp_path), mode="default",
        memory_preamble="", skill_body="",
        tool_count=10, skill_count=58,
    )
    assert "AGENT" in out


def test_build_chat_system_with_auto_mode_keeps_action_block(tmp_path):
    out = executor._build_chat_system(
        workspace=str(tmp_path), mode="auto",
        memory_preamble="", skill_body="",
        tool_count=10, skill_count=58,
    )
    # The auto-mode block from phase 4 stays
    assert "AUTO mode" in out
    # PLUS the agent block from this phase
    assert "AGENT" in out


def test_build_chat_system_skill_body_doesnt_break_agent_directives(tmp_path):
    out = executor._build_chat_system(
        workspace=str(tmp_path), mode="default",
        memory_preamble="", skill_body="A skill that does X.",
        tool_count=0, skill_count=0,
    )
    # Skill body present
    assert "A skill that does X" in out
    # Agent directives still present
    assert "fs_write" in out
