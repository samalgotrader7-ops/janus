"""Tests for v1.17.0 chatbot-vs-agent runtime guard in executor.chat.

The pre-v1.17 chat loop accepted ANY assistant turn without tool_calls as
a final answer — including empty content and "I'll create the file..."
stalls. Smaller models (gpt-oss, qwen, llama-3 8B-class) routinely hit
both failure modes, leaving the user staring at a hang or a broken promise
mid-task (Sam's KV-store benchmark: agent did stage 1+2, regressed in
stage 3, then stopped without writing the rest of the artifacts).

The fix: when no tool_calls AND (content empty OR content is a future-
tense stall), inject a system reminder and retry the SAME step. Bounded
to ONE nudge per chat() call so a model that keeps stalling doesn't
burn the entire MAX_STEPS budget on retries.
"""
from __future__ import annotations

import json

import pytest

from janus import executor, llm
from janus.tools import Registry, Tool
from janus.tools.capabilities import CapabilitySet


# ---------- _looks_like_stall heuristic ----------


@pytest.mark.parametrize("text", [
    "I'll create the file.",
    "I will write that for you.",
    "Let me check the directory.",
    "I'm going to start with stage 1.",
    "I am going to add the test cases now.",
    "I'd be happy to help with that.",
])
def test_stall_phrases_detected(text):
    assert executor._looks_like_stall(text) is True


# Note: permission-asking patterns ("Should I proceed?", "Shall I?",
# "Would you like me to?") are NOT caught by the runtime guard heuristic
# because they end in "?" — and we accept any question as a genuine
# clarifying question rather than a stall, to avoid nudging on requests
# like "Should I use UTF-8 or ASCII?". The system prompt's rule 7
# handles permission-asking in auto/bypass mode at the prompt layer.


@pytest.mark.parametrize("text", [
    "",
    "Done.",
    "wrote /tmp/report.md (8.2 KB)",
    "Tests passed: 12/12.",
    "The function is at parser.py:42.",
])
def test_non_stall_text_not_detected(text):
    assert executor._looks_like_stall(text) is False


def test_stall_with_question_mark_not_detected():
    """Genuine clarifying questions should pass through, not get nudged."""
    assert executor._looks_like_stall("I'll proceed but should I use json?") is False
    # Note: a question mark at the end means the model is asking the user
    # something. The user can answer; nudging would be wrong.


def test_long_text_not_detected_as_stall():
    """A multi-paragraph explanation that happens to contain 'I will' is
    not a stall — it's a deliberate response."""
    text = (
        "There are several approaches to solve this. The classic strategy "
        "involves a hash table with linear probing. I will describe each "
        "tradeoff in detail. First, hash tables offer O(1) amortized lookup. "
        "Second, linear probing has good cache behavior. Third, double "
        "hashing avoids primary clustering. The choice depends on your "
        "workload. " * 2
    )
    assert len(text) > 400
    assert executor._looks_like_stall(text) is False


# ---------- Runtime guard end-to-end (via executor.chat) ----------


class _NoOpTool(Tool):
    """Cheap tool just so registry.schemas() returns something — the
    guard skips when there are no tools."""
    name = "noop"
    description = "no-op"
    parameters = {"type": "object", "properties": {}}
    risk = "read"

    def run(self, args, approver):
        return "ok"


@pytest.fixture
def queued_chat(monkeypatch):
    """Stub llm.chat (and chat_stream-via-fallback) with a response queue.

    Each chat call pops one message dict. Tests build the queue then
    invoke executor.chat with stream=False to use this stub directly.
    """
    queue: list[dict] = []

    def _chat(messages, **kw):
        if not queue:
            raise RuntimeError(
                "queued_chat exhausted — test queued fewer responses than "
                "the chat loop consumed"
            )
        return queue.pop(0)

    monkeypatch.setattr(llm, "chat", _chat)
    return queue


def _approve(*args, **kwargs):
    return True


def _registry_with_one_tool() -> Registry:
    return Registry(tools=[_NoOpTool()])


def test_empty_response_triggers_one_nudge(queued_chat):
    """First turn: empty content + no tool_calls → nudge. Second turn:
    real answer. Loop returns the second answer."""
    queued_chat.append({"role": "assistant", "content": "", "tool_calls": []})
    queued_chat.append({"role": "assistant", "content": "Done.", "tool_calls": []})

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="hi",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    assert output == "Done."
    # Trace should record a 'nudge' step before the final
    nudge_steps = [t for t in trace if t.get("type") == "nudge"]
    assert len(nudge_steps) == 1
    assert nudge_steps[0]["reason"] == "empty"


def test_stall_response_triggers_one_nudge(queued_chat):
    """First turn: stall phrase + no tool_calls → nudge."""
    queued_chat.append({
        "role": "assistant",
        "content": "I'll create the file for you.",
        "tool_calls": [],
    })
    queued_chat.append({
        "role": "assistant", "content": "wrote /tmp/x.md", "tool_calls": [],
    })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="write a file",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    assert output == "wrote /tmp/x.md"
    nudge_steps = [t for t in trace if t.get("type") == "nudge"]
    assert len(nudge_steps) == 1
    assert nudge_steps[0]["reason"] == "stall"


def test_nudge_bounded_to_one_per_chat_call(queued_chat):
    """Even if the model stalls AGAIN after the first nudge, we don't nudge
    a second time — we accept whatever we got and exit. This caps the
    retry budget so a chronically-stalling model can't burn MAX_STEPS."""
    # Three stalls in a row — only the first should trigger a nudge.
    queued_chat.append({"role": "assistant", "content": "I'll do it.", "tool_calls": []})
    queued_chat.append({"role": "assistant", "content": "Let me try.", "tool_calls": []})
    # Important: there should be NO third call. After the second stall
    # (which is post-nudge), the loop accepts and returns.

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    # The post-nudge response is what we return.
    assert output == "Let me try."
    nudges = [t for t in trace if t.get("type") == "nudge"]
    assert len(nudges) == 1


def test_real_answer_no_nudge(queued_chat):
    """First turn already has a real answer → no nudge, no retry."""
    queued_chat.append({
        "role": "assistant",
        "content": "The file is at /tmp/x.md.",
        "tool_calls": [],
    })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="where",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    assert output == "The file is at /tmp/x.md."
    assert not [t for t in trace if t.get("type") == "nudge"]


def test_clarifying_question_no_nudge(queued_chat):
    """Genuine clarifying question (ends with ?) → don't nudge, accept."""
    queued_chat.append({
        "role": "assistant",
        "content": "Should I use UTF-8 or ASCII?",
        "tool_calls": [],
    })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="write the file",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    assert "?" in output
    assert not [t for t in trace if t.get("type") == "nudge"]


def test_no_nudge_when_registry_is_empty(queued_chat, monkeypatch):
    """If the model has no tools (NO_TOOLS=1 mode, or chat-only), nudging
    can't help — there's nothing to call. Skip the nudge, accept the
    response as final."""
    queued_chat.append({"role": "assistant", "content": "", "tool_calls": []})

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="hi",
        tools=Registry(tools=[]),  # empty registry
        approver=_approve, stream=False,
    )

    assert output == ""
    assert not [t for t in trace if t.get("type") == "nudge"]


def test_nudge_message_appended_as_system(queued_chat):
    """The injected nudge should appear as a system-role message in the
    conversation history, so the next LLM call sees it as out-of-band
    instruction rather than user content."""
    queued_chat.append({"role": "assistant", "content": "", "tool_calls": []})
    queued_chat.append({"role": "assistant", "content": "ok", "tool_calls": []})

    messages: list[dict] = []
    executor.chat(
        messages=messages, user_input="x",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    # Find the nudge message — it has role=system and our nudge text.
    nudge_msgs = [
        m for m in messages
        if m.get("role") == "system" and "[system]" in (m.get("content") or "")
    ]
    assert len(nudge_msgs) == 1
    assert "tool" in nudge_msgs[0]["content"].lower()


def test_empty_assistant_turn_dropped_from_messages(queued_chat):
    """The empty/stall assistant message should be removed from the
    visible conversation when we nudge — keeps the history clean and
    avoids the next LLM call seeing 'empty is acceptable'."""
    queued_chat.append({"role": "assistant", "content": "", "tool_calls": []})
    queued_chat.append({"role": "assistant", "content": "ok", "tool_calls": []})

    messages: list[dict] = []
    executor.chat(
        messages=messages, user_input="x",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    # The empty assistant turn should NOT survive in the history.
    empty_assistants = [
        m for m in messages
        if m.get("role") == "assistant" and not (m.get("content") or "")
        and not m.get("tool_calls")
    ]
    assert empty_assistants == []


def test_real_tool_call_no_nudge(queued_chat):
    """A turn with tool_calls should never trigger the nudge — it's
    productive work, not a stall."""
    queued_chat.append({
        "role": "assistant",
        "content": "I'll call the tool now.",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "noop", "arguments": "{}"},
        }],
    })
    queued_chat.append({
        "role": "assistant", "content": "Done.", "tool_calls": [],
    })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="run noop",
        tools=_registry_with_one_tool(), approver=_approve,
        stream=False,
    )

    assert output == "Done."
    assert not [t for t in trace if t.get("type") == "nudge"]
