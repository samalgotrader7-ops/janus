"""Tests for v1.17.2 tool-call recovery from content-leaked JSON.

Sam ran the KV-store benchmark on gpt-oss:120b-cloud and the model emitted
its tool call as raw JSON in the content field instead of a proper
tool_calls field. The chat loop accepted the JSON as a final answer and
the user saw `{"path": "kv_store.py", "content": "import time\\nimport
threading..."}` dumped to chat. This module recovers from that.

The architecturally correct fix is to start the endpoint with
--enable-auto-tool-choice --tool-call-parser hermes (or similar). But
Janus should still work when the endpoint is misconfigured.
"""
from __future__ import annotations
import json

import pytest

from janus import tool_call_recovery, executor, llm
from janus.tools import Registry, Tool
from janus.tools.fs import FsRead, FsWrite


# ---------- Schemas helpers ----------


def _fs_write_schema():
    return {
        "type": "function",
        "function": {
            "name": "fs_write",
            "description": "write a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    }


def _fs_read_schema():
    return {
        "type": "function",
        "function": {
            "name": "fs_read",
            "description": "read a file",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
                "required": ["path"],
            },
        },
    }


def _shell_schema():
    return {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "run shell",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["command"],
            },
        },
    }


# ---------- Shape recovery ----------


def test_recover_fs_write_from_shape_match():
    """The exact bug Sam saw: model emits {"path": ..., "content": ...}
    in content; recovery dispatches to fs_write."""
    content = json.dumps({"path": "kv_store.py", "content": "import time\n"})
    schemas = [_fs_write_schema(), _fs_read_schema(), _shell_schema()]
    out = tool_call_recovery.recover(content, schemas)

    assert out is not None
    assert out["type"] == "function"
    assert out["function"]["name"] == "fs_write"
    args = json.loads(out["function"]["arguments"])
    assert args == {"path": "kv_store.py", "content": "import time\n"}


def test_recover_fs_read_from_shape_match():
    """{"path": "x"} alone matches fs_read (path is the only required arg)."""
    content = json.dumps({"path": "kv_store.py"})
    schemas = [_fs_write_schema(), _fs_read_schema()]
    out = tool_call_recovery.recover(content, schemas)

    assert out is not None
    assert out["function"]["name"] == "fs_read"


def test_recover_strips_json_fences():
    """Markdown-fenced JSON should be unwrapped before parsing."""
    content = '```json\n{"path": "x.py", "content": "..."}\n```'
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is not None
    assert out["function"]["name"] == "fs_write"


def test_recover_strips_bare_fences():
    """Plain ``` fences (no language) also work."""
    content = '```\n{"path": "x.py", "content": "..."}\n```'
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is not None


def test_recover_no_match_for_foreign_keys():
    """If parsed JSON has keys not in any tool schema, no match."""
    content = json.dumps({"path": "x", "extra_field": "stuff"})
    out = tool_call_recovery.recover(content, [_fs_write_schema(), _fs_read_schema()])
    assert out is None


def test_recover_no_match_when_required_missing():
    """fs_write requires path AND content — content alone shouldn't match."""
    content = json.dumps({"content": "hello"})
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is None


def test_recover_picks_most_specific_tool():
    """fs_write (2 required) outranks fs_read (1 required) when keys
    cover both — the more specific match wins."""
    content = json.dumps({"path": "x.py", "content": "..."})
    schemas = [_fs_read_schema(), _fs_write_schema()]
    out = tool_call_recovery.recover(content, schemas)
    assert out["function"]["name"] == "fs_write"


# ---------- Explicit-name shape ----------


def test_recover_explicit_name_arguments():
    """{"name": "fs_write", "arguments": {...}} is a tool-call format
    used by some non-OpenAI providers."""
    content = json.dumps({
        "name": "fs_write",
        "arguments": {"path": "x.py", "content": "..."},
    })
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is not None
    assert out["function"]["name"] == "fs_write"


def test_recover_explicit_arguments_as_string():
    """OpenAI's wire shape has arguments as a JSON string, not dict.
    We unwrap it."""
    content = json.dumps({
        "name": "fs_write",
        "arguments": '{"path": "x.py", "content": "..."}',
    })
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is not None
    args = json.loads(out["function"]["arguments"])
    assert args["path"] == "x.py"


def test_recover_tool_args_shape():
    """Some models emit {"tool": "X", "args": {...}} — also recognized."""
    content = json.dumps({
        "tool": "fs_read",
        "args": {"path": "x.py"},
    })
    out = tool_call_recovery.recover(content, [_fs_read_schema()])
    assert out is not None
    assert out["function"]["name"] == "fs_read"


def test_recover_function_nested_shape():
    """{"function": {"name": "X", "arguments": {...}}} — yet another."""
    content = json.dumps({
        "function": {
            "name": "fs_read",
            "arguments": {"path": "x.py"},
        },
    })
    out = tool_call_recovery.recover(content, [_fs_read_schema()])
    assert out is not None
    assert out["function"]["name"] == "fs_read"


def test_explicit_name_must_match_registered_tool():
    """If model names a tool that isn't registered, refuse to substitute
    another. Better to fail loudly than dispatch the wrong tool."""
    content = json.dumps({
        "name": "fictional_tool",
        "arguments": {"path": "x.py"},
    })
    out = tool_call_recovery.recover(content, [_fs_read_schema(), _fs_write_schema()])
    assert out is None


# ---------- Negative cases ----------


def test_recover_returns_none_for_plain_text():
    """Prose that happens to mention paths is NOT a tool call."""
    content = "I'll create the file at kv_store.py with the content."
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is None


def test_recover_returns_none_for_invalid_json():
    """Mangled JSON: don't try to fix it."""
    content = '{"path": "x", "content": "missing close'
    out = tool_call_recovery.recover(content, [_fs_write_schema()])
    assert out is None


def test_recover_returns_none_for_empty_content():
    out = tool_call_recovery.recover("", [_fs_write_schema()])
    assert out is None


def test_recover_returns_none_with_no_schemas():
    """No registered tools — nothing to dispatch to."""
    out = tool_call_recovery.recover('{"path": "x.py"}', [])
    assert out is None


def test_recover_returns_none_for_array_json():
    """Top-level array is not a tool call (we only accept objects)."""
    out = tool_call_recovery.recover('["a", "b"]', [_fs_write_schema()])
    assert out is None


def test_recover_returns_none_for_paramless_tool():
    """A tool with no parameters would match any JSON — refuse to match
    such tools at all to avoid dispatching arbitrary content to them."""
    paramless = {
        "type": "function",
        "function": {
            "name": "do_thing",
            "description": "no args",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    out = tool_call_recovery.recover('{"x": 1}', [paramless])
    assert out is None


def test_recover_synthesized_call_has_id():
    """The synthesized tool_call must have a unique id (the loop appends
    tool messages with tool_call_id matching this)."""
    content = json.dumps({"path": "x.py"})
    out = tool_call_recovery.recover(content, [_fs_read_schema()])
    assert out is not None
    assert "id" in out
    assert out["id"].startswith("call_recovered_")


# ---------- End-to-end through executor.chat ----------


class _FakeWriteTool(Tool):
    """A fake fs_write that just records the args it got called with."""
    name = "fake_write"
    description = "fake write"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["path", "content"],
    }
    risk = "write"

    def __init__(self):
        self.calls: list[dict] = []

    def run(self, args, approver):
        self.calls.append(dict(args))
        return f"wrote {len(args.get('content', ''))} bytes to {args['path']}"


def _approve(*a, **kw):
    return True


@pytest.fixture
def queued_chat(monkeypatch):
    queue: list[dict] = []

    def _chat(messages, **kw):
        if not queue:
            raise RuntimeError("queue exhausted")
        return queue.pop(0)

    monkeypatch.setattr(llm, "chat", _chat)
    return queue


def test_executor_recovers_content_leaked_tool_call(queued_chat):
    """End-to-end: model emits JSON in content (no tool_calls). Executor
    recovers, dispatches to the matched tool, and the loop continues
    rather than accepting the JSON as final."""
    fake = _FakeWriteTool()
    reg = Registry(tools=[fake])

    # Turn 1: model emits JSON in content (the failure mode).
    queued_chat.append({
        "role": "assistant",
        "content": json.dumps({"path": "kv_store.py", "content": "x = 1\n"}),
        "tool_calls": [],
    })
    # Turn 2: post-tool-call, model gives a final answer.
    queued_chat.append({
        "role": "assistant",
        "content": "Done.",
        "tool_calls": [],
    })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="write kv_store.py",
        tools=reg, approver=_approve, stream=False,
    )

    # The fake tool was actually called with the recovered args.
    assert len(fake.calls) == 1
    assert fake.calls[0] == {"path": "kv_store.py", "content": "x = 1\n"}
    # The loop returned the post-tool final, not the JSON dump.
    assert output == "Done."
    # Trace recorded the recovery.
    recoveries = [t for t in trace if t.get("type") == "recovered_tool_call"]
    assert len(recoveries) == 1
    assert recoveries[0]["tool"] == "fake_write"


def test_executor_no_recovery_for_plain_text(queued_chat):
    """Prose without JSON should NOT trigger recovery; fall through to
    the normal flow (stall nudge in this case)."""
    fake = _FakeWriteTool()
    reg = Registry(tools=[fake])

    # Stall: pure prose, no JSON.
    queued_chat.append({
        "role": "assistant",
        "content": "I'll write the file shortly.",
        "tool_calls": [],
    })
    # Post-nudge final.
    queued_chat.append({
        "role": "assistant",
        "content": "ok",
        "tool_calls": [],
    })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="write kv_store.py",
        tools=reg, approver=_approve, stream=False,
    )

    # No tool actually got called — this was prose, not a leaked call.
    assert fake.calls == []
    assert output == "ok"
    # Stall path fired, not recovery path.
    assert any(t.get("type") == "nudge" for t in trace)
    assert not [t for t in trace if t.get("type") == "recovered_tool_call"]
