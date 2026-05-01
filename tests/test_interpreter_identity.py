"""Tests for v0.13's interpreter self-knowledge fix.

Bug: the interpreter system prompt had no Janus identity, so questions
like "how many tools does Janus have" would be misinterpreted as being
about the Roman god / video game / etc. The fix prepends an identity
block plus a live runtime inventory (tool/skill counts) to the system
prompt on every interpret call.
"""
from __future__ import annotations

import pytest

from janus import interpreter


@pytest.fixture
def captured_system(monkeypatch):
    """Intercept llm.chat and return the system message that was sent."""
    box = {}

    def fake_chat(*, messages, json_mode=False, temperature=0.7, tools=None):
        box["system"] = messages[0]["content"]
        return {"content": '{"interpretations": [{"label":"x","action":"y","risk":"z"}]}'}

    monkeypatch.setattr(interpreter.llm, "chat", fake_chat)
    return box


def test_identity_block_is_in_system_prompt(captured_system):
    interpreter.interpret("hello")
    sys_prompt = captured_system["system"]
    assert "Janus" in sys_prompt
    # The model should be told this is the framework, not the Roman god.
    assert "framework" in sys_prompt.lower()


def test_runtime_inventory_appears_when_counts_passed(captured_system):
    interpreter.interpret("how many tools do you have", tool_count=23, skill_count=4)
    sys_prompt = captured_system["system"]
    assert "23 tool" in sys_prompt
    assert "4 installed skill" in sys_prompt


def test_runtime_inventory_omitted_when_counts_absent(captured_system):
    interpreter.interpret("hello")
    sys_prompt = captured_system["system"]
    # No phantom counts should appear when the caller didn't supply them.
    assert "Right now you have access to" not in sys_prompt


def test_partial_inventory_just_tools(captured_system):
    interpreter.interpret("hi", tool_count=10)
    sys_prompt = captured_system["system"]
    assert "10 tool" in sys_prompt
    assert "installed skill" not in sys_prompt


def test_zero_counts_still_render(captured_system):
    """A fresh install has 0 skills — the model should hear that, not silence."""
    interpreter.interpret("hi", tool_count=23, skill_count=0)
    sys_prompt = captured_system["system"]
    assert "23 tool" in sys_prompt
    assert "0 installed skill" in sys_prompt


def test_memory_preamble_still_works(captured_system):
    """Identity block must not break the existing memory_preamble path."""
    interpreter.interpret("hi", memory_preamble="USER NOTES: Sam likes terse output.")
    sys_prompt = captured_system["system"]
    assert "Sam likes terse output" in sys_prompt
    assert "Janus" in sys_prompt  # both present
