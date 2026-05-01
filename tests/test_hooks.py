"""Tests for Phase 11 — hooks lifecycle.

Hook commands write JSON to stdout; we use small Python one-liners so
tests don't depend on /bin/sh-isms.
"""
from __future__ import annotations
import json
import os
import sys

import pytest

from janus import config, hooks


def _write_hooks_json(janus_home, payload):
    config.HOOKS_FILE.write_text(json.dumps(payload), encoding="utf-8")


def _write_hooks_dir_file(janus_home, name, payload):
    config.HOOKS_DIR.mkdir(parents=True, exist_ok=True)
    (config.HOOKS_DIR / name).write_text(json.dumps(payload), encoding="utf-8")


def _py_hook(body: str) -> str:
    """Build a one-line shell command that pipes stdin into a Python -c
    expression and prints `body` (a Python expression returning a string)."""
    code = f"import sys, json; d = json.loads(sys.stdin.read()); print({body})"
    return f"{sys.executable} -c \"{code}\""


# ---------- load_hooks ----------


def test_load_hooks_no_config(janus_home):
    out = hooks.load_hooks()
    assert all(out[ev] == [] for ev in hooks.ALL_EVENTS)


def test_load_hooks_single_file_form(janus_home):
    _write_hooks_json(janus_home, {
        "hooks": {
            "PreToolUse": [
                {"command": "echo x", "matcher": "shell"},
            ]
        }
    })
    out = hooks.load_hooks()
    assert len(out["PreToolUse"]) == 1
    assert out["PreToolUse"][0].matcher == "shell"


def test_load_hooks_dir_form_with_event_field(janus_home):
    _write_hooks_dir_file(janus_home, "01.shellguard.json", {
        "event": "PreToolUse", "command": "echo x", "matcher": "shell",
    })
    out = hooks.load_hooks()
    assert len(out["PreToolUse"]) == 1


def test_load_hooks_skips_invalid_event(janus_home):
    _write_hooks_json(janus_home, {
        "hooks": {"NotARealEvent": [{"command": "x"}]}
    })
    out = hooks.load_hooks()
    assert all(out[ev] == [] for ev in hooks.ALL_EVENTS)


def test_load_hooks_skips_missing_command(janus_home):
    _write_hooks_json(janus_home, {
        "hooks": {"PreToolUse": [{"matcher": "x"}]}
    })
    assert hooks.load_hooks()["PreToolUse"] == []


# ---------- matcher ----------


def test_hook_matches_when_no_matcher():
    h = hooks.Hook(event="PreToolUse", command="x", matcher="")
    assert h.matches("anything") is True


def test_hook_matches_regex():
    h = hooks.Hook(event="PreToolUse", command="x", matcher="^shell$")
    assert h.matches("shell") is True
    assert h.matches("fs_shell") is False


def test_hook_matcher_invalid_regex_does_not_match():
    h = hooks.Hook(event="PreToolUse", command="x", matcher="(unclosed")
    assert h.matches("shell") is False


# ---------- HookDecision ----------


def test_hook_decision_from_dict_allow():
    d = hooks.HookDecision.from_dict({"decision": "allow"})
    assert d.allow is True
    assert d.reason == ""


def test_hook_decision_from_dict_deny():
    d = hooks.HookDecision.from_dict({"decision": "deny", "reason": "nope"})
    assert d.allow is False
    assert d.reason == "nope"


def test_hook_decision_merge_any_deny_wins():
    a = hooks.HookDecision(allow=True, injected_context="A")
    b = hooks.HookDecision(allow=False, reason="b's reason")
    c = a.merge(b)
    assert c.allow is False
    assert c.reason == "b's reason"


def test_hook_decision_merge_concatenates_context():
    a = hooks.HookDecision(injected_context="alpha")
    b = hooks.HookDecision(injected_context="beta")
    c = a.merge(b)
    assert "alpha" in c.injected_context
    assert "beta" in c.injected_context


# ---------- fire ----------


def test_fire_no_hooks_returns_default_allow(janus_home):
    d = hooks.fire("PreToolUse", {"tool": "shell"}, match_field="tool")
    assert d.allow is True
    assert d.reason == ""


def test_fire_runs_subprocess_and_parses_decision(janus_home):
    # Hook prints {"decision":"deny","reason":"because"}.
    _write_hooks_json(janus_home, {
        "hooks": {"PreToolUse": [{
            "command": _py_hook("'\\\"decision\\\": \\\"deny\\\", \\\"reason\\\": \\\"because\\\"'"),
        }]}
    })
    # Easier: write a hook file via dir form using a here-doc in Python.
    config.HOOKS_FILE.unlink()
    _write_hooks_dir_file(janus_home, "deny.json", {
        "event": "PreToolUse",
        "command": (
            f"{sys.executable} -c \"import json; "
            f"print(json.dumps({{'decision': 'deny', 'reason': 'because'}}))\""
        ),
    })
    d = hooks.fire("PreToolUse", {"tool": "shell"}, match_field="tool")
    assert d.allow is False
    assert d.reason == "because"


def test_fire_only_runs_matching_hooks(janus_home):
    _write_hooks_dir_file(janus_home, "shell-only.json", {
        "event": "PreToolUse",
        "command": (
            f"{sys.executable} -c \"import json; "
            f"print(json.dumps({{'decision': 'deny', 'reason': 'shell-blocked'}}))\""
        ),
        "matcher": "^shell$",
    })
    # Match: should deny.
    d1 = hooks.fire("PreToolUse", {"tool": "shell"}, match_field="tool")
    assert d1.allow is False
    # No match: should allow.
    d2 = hooks.fire("PreToolUse", {"tool": "fs_read"}, match_field="tool")
    assert d2.allow is True


def test_fire_exit_code_one_denies(janus_home):
    """Hooks that emit nothing on stdout but exit 1 are treated as deny."""
    _write_hooks_dir_file(janus_home, "exit1.json", {
        "event": "PreToolUse",
        "command": f"{sys.executable} -c \"import sys; sys.exit(1)\"",
    })
    d = hooks.fire("PreToolUse", {"tool": "x"}, match_field="tool")
    assert d.allow is False


def test_fire_uses_hooks_index_when_provided(janus_home):
    """Pass an empty hooks_index to skip filesystem load (perf path)."""
    _write_hooks_dir_file(janus_home, "deny.json", {
        "event": "PreToolUse",
        "command": f"{sys.executable} -c \"import sys; sys.exit(1)\"",
    })
    empty = {ev: [] for ev in hooks.ALL_EVENTS}
    d = hooks.fire("PreToolUse", {"tool": "x"}, match_field="tool",
                   hooks_index=empty)
    assert d.allow is True  # because we passed an empty index


# ---------- Executor integration ----------


def test_executor_pretooluse_hook_denies_tool_call(janus_home, fake_llm):
    """Fire path: a denying PreToolUse hook should make the tool result a
    refusal string the model sees."""
    from janus import executor
    from janus.tools.base import Tool, Registry

    # Configure a deny hook.
    _write_hooks_dir_file(janus_home, "deny-shell.json", {
        "event": "PreToolUse",
        "command": (
            f"{sys.executable} -c \"import json; "
            f"print(json.dumps({{'decision': 'deny', 'reason': 'no_shell'}}))\""
        ),
    })

    class _NoopTool(Tool):
        name = "noop"
        description = "noop"
        parameters = {"type": "object", "properties": {}}
        dangerous = False
        def __init__(self):
            self.calls = 0
        def run(self, args, approver):
            self.calls += 1
            return "should-not-run"

    tool = _NoopTool()
    reg = Registry([tool])

    # First LLM turn: requests a tool call. Second: returns final text.
    fake_llm.append({
        "content": "",
        "tool_calls": [{
            "id": "1",
            "function": {"name": "noop", "arguments": "{}"},
        }],
    })
    fake_llm.append({"content": "done", "tool_calls": []})

    output, trace = executor.execute(
        original_request="x", chosen_label="x", chosen_action="x",
        tools=reg, approver=lambda *a, **kw: True,
    )
    # Tool was NEVER actually invoked (hook denied).
    assert tool.calls == 0
    # Trace records the denial.
    assert any(s.get("hook_denied") for s in trace if s.get("type") == "tool_call")
