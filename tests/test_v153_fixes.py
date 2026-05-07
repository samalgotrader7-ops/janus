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
    LLM call inside the per-step loop. This fires every iteration, so
    step 1+ get an indicator (step 0 is covered by the CLI's pre-call
    ⚡ thinking).

    v1.20: the loop changed from `for step in range(config.MAX_STEPS)`
    to `for step in itertools.count()` because the budget became
    dynamic (soft cap + progress extension + user continuation gate).
    Match either shape for forward compatibility.
    """
    src = inspect.getsource(executor.chat)
    # The model_start emit
    assert "model_start" in src
    # Must be inside a for-step loop AND before llm.chat_stream.
    loop_idx = src.find("for step in itertools.count()")
    if loop_idx < 0:
        loop_idx = src.find("for step in range(config.MAX_STEPS):")
    model_start_idx = src.find('"model_start"')
    chat_stream_idx = src.find("llm.chat_stream(")
    assert loop_idx >= 0, "expected a per-step loop in chat()"
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
    """Don't say 'I'll write it' then end turn without fs_write.
    v1.26.0: the rule lives as a bullet in section 2 (Tool selection)."""
    s = executor.JANUS_CHAT_SYSTEM
    rule_idx = s.find("File-creation requests")
    assert rule_idx >= 0, "expected the file-creation bullet in section 2"
    # Span the bullet body up to the next bullet or the next section.
    end = s.find("Comparison / report / analysis", rule_idx)
    if end == -1:
        end = rule_idx + 1500
    body = s[rule_idx:end]
    assert "SAME TURN" in body or "same turn" in body.lower()


# ---------- Comprehensive/detailed = full document ----------
# (Was rule 2b in pre-v1.26 numbering. v1.26.0 folds this into the
#  Tone section's last bullet so the contrast with "tight chat
#  replies" stays visible. Tests below pin the bullet's content
#  rather than its old "2b" marker.)


def test_full_document_rule_lists_keywords():
    s = executor.JANUS_CHAT_SYSTEM
    keywords = ("comprehensive", "detailed", "full", "in-depth", "thorough")
    matches = sum(1 for k in keywords if k in s.lower())
    assert matches >= 3


def test_full_document_rule_says_full_document_not_stub():
    s = executor.JANUS_CHAT_SYSTEM
    assert "FULL document" in s or "full document" in s.lower()
    # Anti-pattern wording
    assert "stub" in s.lower() or "5 lines" in s.lower()


def test_full_document_rule_clarifies_brevity_dont_apply_to_files():
    """Brevity is about CHAT REPLY, not file content. v1.26.0 anchors
    on the bullet that lists the keywords."""
    s = executor.JANUS_CHAT_SYSTEM
    idx = s.find("Comprehensive")
    assert idx >= 0, "expected the comprehensive/detailed bullet"
    body = s[idx:idx + 800]
    assert "CHAT REPLY" in body or "chat reply" in body.lower()
    assert "NOT" in body or "not to" in body.lower()
    assert "file content" in body.lower() or "files" in body.lower()


def test_full_document_rule_gives_size_target():
    """Concrete number so the model has a target, not a vague 'long'."""
    s = executor.JANUS_CHAT_SYSTEM
    idx = s.find("Comprehensive")
    body = s[idx:idx + 800]
    # Should mention a KB target (e.g., "5 KB", "5-20 KB")
    assert "KB" in body


# ---------- Brevity scope clarifications (was rules 6 / 9) ----------
# Both the "tight chat reply" rule and the "answer directly" rule live
# in section 1 (Tone) of the v1.26.0 prompt. The pin is that BOTH must
# scope themselves to CHAT REPLIES so the model doesn't apply brevity
# to file content it writes.


def test_tight_chat_reply_rule_scoped_to_chat_reply():
    s = executor.JANUS_CHAT_SYSTEM
    idx = s.find("Chat replies are tight")
    assert idx >= 0, "expected the tight-chat-reply bullet"
    body = s[idx:idx + 700]
    assert "CHAT REPLY" in body or "chat reply" in body.lower()
    # Scope clarification must be present
    assert (
        "not file" in body.lower()
        or "not about" in body.lower()
        or "not content" in body.lower()
        or "NOT TO FILE" in body
        or "not to file" in body.lower()
    )


def test_answer_directly_rule_scoped_to_chat_reply():
    s = executor.JANUS_CHAT_SYSTEM
    idx = s.find("Answer questions DIRECTLY")
    assert idx >= 0, "expected the answer-directly bullet"
    # Span the bullet body up to the next bullet header (file:line refs).
    end = s.find("File:line references", idx)
    if end == -1:
        end = idx + 1000
    body = s[idx:end]
    assert (
        "CHAT REPLY" in body
        or "chat replies" in body.lower()
        or "chat reply" in body.lower()
    )
