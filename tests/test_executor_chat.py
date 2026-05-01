"""Tests for executor.chat() — the v1.0 Claude-Code-shaped loop.

Coverage:
- single-turn echo (no tool calls): system message present, user
  appended, assistant appended, returns assistant text
- multi-turn: messages list grows monotonically, system message is
  refreshed at index 0 each turn
- tool call cycle: approver-allowed tool result appended, model loops,
  returns final text
- mode is reflected in the system prompt (plan mode adds the
  "denied" warning so the model can adapt)
- memory_preamble is prepended above the chat system block
"""
from __future__ import annotations

from janus import executor, permissions
from janus.tools.base import Registry, Tool


def _stub_llm(monkeypatch, responses):
    """Replace janus.llm.chat with a queue-driven stub. Streaming is
    bypassed by setting stream=False in the chat() call."""
    import janus.llm
    queue = list(responses)

    def chat(messages, tools=None, json_mode=False, temperature=0.7):
        if not queue:
            raise RuntimeError("stub queue empty")
        return queue.pop(0)

    monkeypatch.setattr(janus.llm, "chat", chat)
    return queue


def _yes_approver(label, details, **kw):
    return True


def test_single_turn_echo_appends_system_user_assistant(monkeypatch, janus_home):
    _stub_llm(monkeypatch, [
        {"role": "assistant", "content": "hi back"},
    ])
    msgs: list[dict] = []
    out, trace = executor.chat(
        messages=msgs, user_input="hi",
        tools=Registry([]), approver=_yes_approver,
        stream=False,
    )
    assert out == "hi back"
    # system + user + assistant
    assert len(msgs) == 3
    assert msgs[0]["role"] == "system"
    assert msgs[1] == {"role": "user", "content": "hi"}
    assert msgs[2]["role"] == "assistant"
    # final-step trace
    assert trace and trace[-1]["type"] == "final"


def test_multi_turn_messages_persist_across_calls(monkeypatch, janus_home):
    _stub_llm(monkeypatch, [
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "second"},
    ])
    msgs: list[dict] = []
    out1, _ = executor.chat(
        messages=msgs, user_input="one",
        tools=Registry([]), approver=_yes_approver, stream=False,
    )
    assert out1 == "first"
    out2, _ = executor.chat(
        messages=msgs, user_input="two",
        tools=Registry([]), approver=_yes_approver, stream=False,
    )
    assert out2 == "second"
    # system + user1 + assistant1 + user2 + assistant2
    assert len(msgs) == 5
    assert msgs[1]["content"] == "one"
    assert msgs[3]["content"] == "two"


def test_system_message_refreshed_each_turn(monkeypatch, janus_home):
    _stub_llm(monkeypatch, [
        {"role": "assistant", "content": "ok"},
        {"role": "assistant", "content": "ok"},
    ])
    msgs: list[dict] = []
    executor.chat(
        messages=msgs, user_input="x",
        tools=Registry([]), approver=_yes_approver,
        mode=permissions.DEFAULT, stream=False,
    )
    sys_default = msgs[0]["content"]
    executor.chat(
        messages=msgs, user_input="y",
        tools=Registry([]), approver=_yes_approver,
        mode=permissions.PLAN, stream=False,
    )
    sys_plan = msgs[0]["content"]
    assert "PLAN mode" in sys_plan
    assert sys_plan != sys_default


def test_tool_call_cycle_appends_tool_result_then_loops(monkeypatch, janus_home):
    """The model first emits a tool_call; we run the tool, append the
    tool message, the model emits final text on the next pass."""

    class Echo(Tool):
        name = "echo"
        description = "echo back"
        parameters = {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        }
        dangerous = False
        risk = "read"

        def run(self, args, approver):
            return f"echoed: {args.get('text', '')}"

    _stub_llm(monkeypatch, [
        # Turn 1: model wants the echo tool
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "t0",
                "type": "function",
                "function": {"name": "echo", "arguments": '{"text": "hello"}'},
            }],
        },
        # Turn 2: model produces final text
        {"role": "assistant", "content": "done: echoed hello"},
    ])
    msgs: list[dict] = []
    out, trace = executor.chat(
        messages=msgs, user_input="say hi",
        tools=Registry([Echo()]), approver=_yes_approver,
        stream=False,
    )
    assert out == "done: echoed hello"
    # system + user + assistant_with_call + tool_result + assistant_final
    roles = [m.get("role") for m in msgs]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    # Tool result message must carry the tool_call_id Anthropic/OpenAI expect.
    assert msgs[3] == {
        "role": "tool", "tool_call_id": "t0",
        "content": "echoed: hello",
    }
    # Trace records tool_call entry.
    tool_steps = [s for s in trace if s.get("type") == "tool_call"]
    assert len(tool_steps) == 1 and tool_steps[0]["tool"] == "echo"


def test_memory_preamble_prepended_to_system(monkeypatch, janus_home):
    _stub_llm(monkeypatch, [{"role": "assistant", "content": "ok"}])
    msgs: list[dict] = []
    executor.chat(
        messages=msgs, user_input="x",
        tools=Registry([]), approver=_yes_approver,
        memory_preamble="# user model\n\nuser likes terse output",
        stream=False,
    )
    sys = msgs[0]["content"]
    assert "user likes terse output" in sys
    # And the chat system block still follows.
    assert "Janus" in sys


def test_skill_body_appears_in_system(monkeypatch, janus_home):
    _stub_llm(monkeypatch, [{"role": "assistant", "content": "ok"}])
    msgs: list[dict] = []
    executor.chat(
        messages=msgs, user_input="x",
        tools=Registry([]), approver=_yes_approver,
        skill_body="run pnpm test before declaring done",
        stream=False,
    )
    assert "Active skill" in msgs[0]["content"]
    assert "pnpm test" in msgs[0]["content"]


def test_workspace_and_mode_in_system(monkeypatch, janus_home):
    _stub_llm(monkeypatch, [{"role": "assistant", "content": "ok"}])
    msgs: list[dict] = []
    executor.chat(
        messages=msgs, user_input="x",
        tools=Registry([]), approver=_yes_approver,
        workspace="/tmp/project",
        mode=permissions.ACCEPT_EDITS,
        stream=False,
    )
    sys = msgs[0]["content"]
    assert "/tmp/project" in sys
    assert "acceptEdits" in sys
