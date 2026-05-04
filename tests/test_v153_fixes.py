"""Tests for v1.5.3 fixes:
  - executor heartbeat at every step boundary (not just step 0)
  - cli_rich step renderer handles model_start
  - system prompt: brevity rules apply to CHAT, not file content
  - Rule 1: anti-pattern + RIGHT example
  - Rule 2b: comprehensive/detailed → write the FULL document
"""
from __future__ import annotations
import inspect

from janus import executor


# ---------- Heartbeat at every step ----------


def test_executor_emits_model_start_at_each_step():
    """Source-level pin: chat() emits a model_start step BEFORE the
    LLM call inside the for-loop. This fires every iteration, so step 1+
    get an indicator (step 0 is covered by the CLI's pre-call ⚡ thinking)."""
    src = inspect.getsource(executor.chat)
    # The model_start emit
    assert "model_start" in src
    # Must be inside the for-step loop AND before llm.chat_stream
    loop_idx = src.find("for step in range(config.MAX_STEPS):")
    model_start_idx = src.find('"model_start"')
    chat_stream_idx = src.find("llm.chat_stream(")
    assert loop_idx >= 0
    assert model_start_idx >= 0
    assert chat_stream_idx >= 0
    assert loop_idx < model_start_idx < chat_stream_idx, (
        "model_start must be inside the loop AND emitted BEFORE chat_stream"
    )


def test_executor_model_start_has_step_field():
    """The emit dict must include the step number so renderers can
    differentiate step 0 (already covered by CLI ⚡ thinking) from N+1."""
    src = inspect.getsource(executor.chat)
    # Look for the on_step call with model_start
    # Pattern: on_step({"step": step, "type": "model_start"})
    assert '"step": step' in src and '"type": "model_start"' in src


# ---------- cli_rich step renderer handles model_start ----------


def test_cli_rich_renders_model_start():
    from janus import cli_rich
    src = inspect.getsource(cli_rich._render_step_factory)
    assert "model_start" in src
    # Renders a heartbeat line
    assert "calling model" in src or "step" in src.lower()


def test_cli_rich_skips_model_start_for_step_0():
    """Step 0's heartbeat is the CLI's pre-call '⚡ thinking…' message;
    showing model_start ALSO for step 0 would be redundant."""
    from janus import cli_rich
    src = inspect.getsource(cli_rich._render_step_factory)
    # The renderer must guard step_num > 0 before printing
    assert "step_num > 0" in src or "step >= 1" in src or "step > 0" in src


# ---------- System prompt: Rule 1 anti-pattern ----------


def test_rule_1_includes_wrong_anti_pattern():
    s = executor.JANUS_CHAT_SYSTEM
    assert "❌ WRONG" in s or "WRONG" in s


def test_rule_1_includes_right_pattern_example():
    s = executor.JANUS_CHAT_SYSTEM
    assert "✅ RIGHT" in s or "RIGHT" in s


def test_rule_1_anti_pattern_uses_concrete_example():
    s = executor.JANUS_CHAT_SYSTEM
    # Anti-pattern: "I'll create" or "I'll write" without fs_write
    assert "I'll create" in s or "I'll write" in s


def test_rule_1_says_full_content_in_same_turn():
    """Don't say 'I'll write it' then end turn without fs_write."""
    s = executor.JANUS_CHAT_SYSTEM
    rule1_idx = s.find("1. **When the user asks you to write")
    rule2_idx = s.find("2. **")
    rule1_body = s[rule1_idx:rule2_idx]
    assert "SAME TURN" in rule1_body or "same turn" in rule1_body.lower()


# ---------- Rule 2b: comprehensive/detailed = full document ----------


def test_rule_2b_exists():
    s = executor.JANUS_CHAT_SYSTEM
    assert "2b" in s


def test_rule_2b_lists_keywords():
    s = executor.JANUS_CHAT_SYSTEM
    keywords = ("comprehensive", "detailed", "full", "in-depth", "thorough")
    matches = sum(1 for k in keywords if k in s.lower())
    assert matches >= 3


def test_rule_2b_says_full_document_not_stub():
    s = executor.JANUS_CHAT_SYSTEM
    assert "FULL document" in s or "full document" in s.lower()
    # Anti-pattern wording
    assert "stub" in s.lower() or "5 lines" in s.lower()


def test_rule_2b_clarifies_brevity_rules_dont_apply_to_files():
    s = executor.JANUS_CHAT_SYSTEM
    # Must explicitly say brevity is about CHAT REPLY, not file content
    rule2b_idx = s.find("2b")
    assert rule2b_idx >= 0
    body = s[rule2b_idx:rule2b_idx + 800]
    assert "CHAT REPLY" in body or "chat reply" in body.lower()
    assert "NOT" in body
    assert "file content" in body.lower() or "files" in body.lower()


def test_rule_2b_gives_size_target():
    """Concrete number so the model has a target, not a vague 'long'."""
    s = executor.JANUS_CHAT_SYSTEM
    rule2b_idx = s.find("2b")
    body = s[rule2b_idx:rule2b_idx + 800]
    # Should mention a KB target (e.g., "5 KB", "5-20 KB")
    assert "KB" in body


# ---------- Rule 6 / Rule 9 scope clarification ----------


def test_rule_6_scoped_to_chat_reply():
    s = executor.JANUS_CHAT_SYSTEM
    rule6_idx = s.find("6. **")
    rule7_idx = s.find("7. **")
    rule6_body = s[rule6_idx:rule7_idx]
    assert "CHAT REPLY" in rule6_body or "chat reply" in rule6_body.lower()
    assert "not file" in rule6_body.lower() or "not about" in rule6_body.lower() or "not content" in rule6_body.lower()


def test_rule_9_scoped_to_chat_reply():
    s = executor.JANUS_CHAT_SYSTEM
    rule9_idx = s.find("9. **")
    # Find rule end (next rule or end of section)
    rule_end = s.find("# WHEN CHAT IS APPROPRIATE", rule9_idx)
    if rule_end == -1:
        rule_end = rule9_idx + 1000
    rule9_body = s[rule9_idx:rule_end]
    assert "CHAT REPLY" in rule9_body or "chat replies" in rule9_body.lower() or "chat reply" in rule9_body.lower()
