"""Tests for v1.20 step-budget redesign.

The pre-v1.20 chat loop terminated at config.MAX_STEPS=25 with no soft
warning, no progress-awareness, and no continuation gate — a circuit
breaker that tripped during normal operation on multi-stage tasks. The
new design has soft cap (warn-then-continue), progress-aware extension
(productive write/exec results push the runway out), and a user/auto
continuation gate at the hard cap.

These tests cover the runtime contract end-to-end via executor.chat.
"""
from __future__ import annotations

import pytest

from janus import config, executor, llm
from janus.tools import Registry, Tool


# ---------- _is_productive helper ----------


class _ReadTool(Tool):
    name = "tool_read"
    description = "read"
    parameters = {"type": "object", "properties": {}}
    risk = "read"

    def run(self, args, approver):
        return "ok"


class _WriteTool(Tool):
    name = "tool_write"
    description = "write"
    parameters = {"type": "object", "properties": {}}
    risk = "write"

    def run(self, args, approver):
        return "wrote /tmp/x.md (12 bytes)"


class _ExecTool(Tool):
    name = "tool_exec"
    description = "exec"
    parameters = {"type": "object", "properties": {}}
    risk = "exec"

    def run(self, args, approver):
        return "exit 0"


class _ErrorTool(Tool):
    name = "tool_err"
    description = "always errors"
    parameters = {"type": "object", "properties": {}}
    risk = "write"

    def run(self, args, approver):
        return "[error] permission denied"


def _registry(*tools: Tool) -> Registry:
    return Registry(tools=list(tools))


def test_read_tool_not_productive():
    reg = _registry(_ReadTool(), _WriteTool())
    assert executor._is_productive("tool_read", "ok", reg) is False


def test_write_tool_productive_when_no_error():
    reg = _registry(_ReadTool(), _WriteTool())
    assert executor._is_productive("tool_write", "wrote 100 bytes", reg) is True


def test_exec_tool_productive_when_no_error():
    reg = _registry(_ExecTool())
    assert executor._is_productive("tool_exec", "exit 0", reg) is True


def test_write_tool_with_error_not_productive():
    reg = _registry(_ErrorTool())
    assert executor._is_productive("tool_err", "[error] denied", reg) is False
    assert executor._is_productive("tool_err", "refused by hook", reg) is False
    assert executor._is_productive("tool_err", "Error: file missing", reg) is False


def test_unknown_tool_not_productive():
    reg = _registry(_WriteTool())
    assert executor._is_productive("nonexistent", "ok", reg) is False


# ---------- _try_extend_budget helper ----------


def test_extend_plan_mode_never_extends():
    granted, reason = executor._try_extend_budget(
        mode="plan", approver=lambda *a, **k: True,
        step=200, hard_cap=200, productive_count=10,
        already_auto_extended=False,
    )
    assert granted is False
    assert reason == "plan_mode"


def test_extend_auto_mode_extends_once_with_progress():
    granted, reason = executor._try_extend_budget(
        mode="auto", approver=None,
        step=200, hard_cap=200, productive_count=5,
        already_auto_extended=False,
    )
    assert granted is True
    assert reason == "auto_extended"


def test_extend_auto_mode_refuses_second_extension():
    granted, reason = executor._try_extend_budget(
        mode="auto", approver=None,
        step=400, hard_cap=200, productive_count=10,
        already_auto_extended=True,
    )
    assert granted is False
    assert reason == "auto_already_extended"


def test_extend_auto_mode_refuses_when_no_progress():
    granted, reason = executor._try_extend_budget(
        mode="auto", approver=None,
        step=200, hard_cap=200, productive_count=0,
        already_auto_extended=False,
    )
    assert granted is False
    assert reason == "no_progress"


def test_extend_default_mode_calls_approver():
    calls = []

    def fake_approver(label, details, **kw):
        calls.append((label, details, kw.get("risk")))
        return True

    granted, reason = executor._try_extend_budget(
        mode="default", approver=fake_approver,
        step=200, hard_cap=200, productive_count=3,
        already_auto_extended=False,
    )
    assert granted is True
    assert reason == "user_granted"
    assert len(calls) == 1
    label, details, risk = calls[0]
    assert "extend" in label.lower()
    assert "200" in details
    assert risk == "ask"


def test_extend_default_mode_user_denies():
    granted, reason = executor._try_extend_budget(
        mode="default", approver=lambda *a, **k: False,
        step=200, hard_cap=200, productive_count=3,
        already_auto_extended=False,
    )
    assert granted is False
    assert reason == "user_denied"


def test_extend_default_mode_no_approver():
    granted, reason = executor._try_extend_budget(
        mode="default", approver=None,
        step=200, hard_cap=200, productive_count=3,
        already_auto_extended=False,
    )
    assert granted is False
    assert reason == "no_approver"


def test_extend_approver_exception_treated_as_denial():
    def boom(*a, **k):
        raise RuntimeError("approver failed")

    granted, reason = executor._try_extend_budget(
        mode="default", approver=boom,
        step=200, hard_cap=200, productive_count=3,
        already_auto_extended=False,
    )
    assert granted is False
    assert reason == "approver_error"


# ---------- soft-cap reminder content ----------


def test_soft_cap_reminder_mentions_step_and_hard_cap():
    msg = executor._build_soft_cap_reminder(step=50, hard_cap=200)
    assert "50" in msg
    assert "200" in msg
    assert "wrap" in msg.lower() or "wrapping" in msg.lower()


# ---------- end-to-end via executor.chat ----------


@pytest.fixture
def queued_chat(monkeypatch):
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


def test_normal_run_does_not_trigger_soft_cap(queued_chat):
    """Steps 0-2 well under the soft cap → no reminder, no extension."""
    queued_chat.append({"role": "assistant", "content": "Done.", "tool_calls": []})

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="hi",
        tools=_registry(_ReadTool()), approver=_approve,
        stream=False,
    )
    assert output == "Done."
    assert not [t for t in trace if t.get("type") == "soft_cap_warning"]
    assert not [t for t in trace if t.get("type") == "step_limit_reached"]


def test_soft_cap_reminder_fires_once_at_threshold(monkeypatch, queued_chat):
    """Cross the soft cap → exactly one soft_cap_warning trace entry."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 2)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 10)

    # 4 turns, all read-tool calls so no progress extension.
    for _ in range(4):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "tool_read", "arguments": "{}"},
            }],
        })
    queued_chat.append({"role": "assistant", "content": "Done.", "tool_calls": []})

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="loop",
        tools=_registry(_ReadTool()), approver=_approve,
        stream=False,
    )
    assert output == "Done."
    warnings = [t for t in trace if t.get("type") == "soft_cap_warning"]
    assert len(warnings) == 1, f"expected exactly 1 soft_cap_warning, got {len(warnings)}"
    assert warnings[0]["soft_cap"] == 2
    assert warnings[0]["hard_cap"] == 10


def test_productive_write_tool_extends_runway(monkeypatch, queued_chat):
    """A successful write-tool call extends current_cap by progress_grace.

    Setup: hard_cap=2 (would normally stop after 2 steps). Tool is a
    write-tool returning success. Second step should still execute
    because productive_count==1 extended the cap by progress_grace.
    """
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)  # disable soft cap
    monkeypatch.setattr(config, "STEP_HARD_CAP", 2)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 5)

    # Need enough turns to exceed original hard_cap=2 and rely on extension
    for _ in range(4):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{_}", "type": "function",
                "function": {"name": "tool_write", "arguments": "{}"},
            }],
        })
    queued_chat.append({"role": "assistant", "content": "Done.", "tool_calls": []})

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="write things",
        tools=_registry(_WriteTool()), approver=_approve,
        stream=False,
    )
    assert output == "Done."
    extensions = [t for t in trace if t.get("type") == "progress_extension"]
    assert len(extensions) >= 1
    assert all(e["new_cap"] > 2 for e in extensions)


def test_error_result_does_not_extend_runway(monkeypatch, queued_chat):
    """Tool returns "[error] ..." → not a productive milestone, no
    extension. With hard_cap=2 the loop should hit step_limit at step 2.
    """
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 2)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 5)

    # 3 turns of error tool calls — no productive extension. Plan mode
    # so the hard-cap continuation gate ALSO refuses (no user prompt).
    for i in range(3):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_err", "arguments": "{}"},
            }],
        })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_ErrorTool()), approver=_approve,
        stream=False,
        mode="plan",  # plan never extends → terminates cleanly at hard_cap
    )
    assert "stopped" in output and "step limit" in output
    extensions = [t for t in trace if t.get("type") == "progress_extension"]
    assert extensions == []
    limit = [t for t in trace if t.get("type") == "step_limit_reached"]
    assert len(limit) == 1
    assert limit[0]["reason"] == "plan_mode"


def test_hard_cap_continuation_default_mode_user_approves(monkeypatch, queued_chat):
    """At hard cap in default mode, approver is called. If it returns
    True, current_cap is bumped and the loop continues."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 2)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 0)

    # 3 read-tool calls (no progress extension), then final.
    for i in range(3):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_read", "arguments": "{}"},
            }],
        })
    queued_chat.append({"role": "assistant", "content": "Done.", "tool_calls": []})

    approver_calls = []

    def approver(label, details, **kw):
        approver_calls.append(label)
        # Approve any tool call AND the extend prompt.
        return True

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_ReadTool()), approver=approver,
        stream=False,
        mode="default",
    )
    assert output == "Done."
    extensions = [t for t in trace if t.get("type") == "budget_extended"]
    assert len(extensions) >= 1
    assert any(e["reason"] == "user_granted" for e in extensions)
    # Approver was called for the extension prompt
    assert any("extend" in c.lower() for c in approver_calls)


def test_hard_cap_continuation_default_mode_user_denies(monkeypatch, queued_chat):
    """At hard cap, user denies → terminate cleanly with step_limit_reached."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 2)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 0)

    for i in range(3):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_read", "arguments": "{}"},
            }],
        })

    def approver(label, details, **kw):
        # Approve normal tool calls; deny extension.
        if "extend" in label.lower():
            return False
        return True

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_ReadTool()), approver=approver,
        stream=False,
        mode="default",
    )
    assert "stopped" in output and "step limit" in output
    limit = [t for t in trace if t.get("type") == "step_limit_reached"]
    assert len(limit) == 1
    assert limit[0]["reason"] == "user_denied"


def test_hard_cap_auto_mode_extends_once_then_terminates(monkeypatch, queued_chat):
    """Auto mode: at hard_cap with progress, auto-extend once. Hit the
    extended cap → terminate (no second auto-extension)."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 2)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 0)

    # Need more steps than 2 + 2 (extended) = 4 to force second cap hit.
    for i in range(8):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_write", "arguments": "{}"},
            }],
        })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_WriteTool()), approver=_approve,
        stream=False,
        mode="auto",
    )
    auto_extensions = [
        t for t in trace
        if t.get("type") == "budget_extended" and t.get("reason") == "auto_extended"
    ]
    assert len(auto_extensions) == 1, (
        f"auto mode should extend exactly once, got {len(auto_extensions)}"
    )
    limit = [t for t in trace if t.get("type") == "step_limit_reached"]
    assert len(limit) == 1
    assert limit[0]["reason"] == "auto_already_extended"


def test_hard_cap_plan_mode_terminates_immediately(monkeypatch, queued_chat):
    """Plan mode never extends, regardless of progress."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 2)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 0)

    for i in range(4):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_write", "arguments": "{}"},
            }],
        })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_WriteTool()), approver=_approve,
        stream=False,
        mode="plan",
    )
    assert "stopped" in output
    limit = [t for t in trace if t.get("type") == "step_limit_reached"]
    assert len(limit) == 1
    assert limit[0]["reason"] == "plan_mode"
    extensions = [t for t in trace if t.get("type") == "budget_extended"]
    assert extensions == []


def test_progress_grace_capped_at_2x_hard_cap(monkeypatch, queued_chat):
    """Productive milestones extend current_cap, but only up to 2× hard_cap.
    User extension is the only path past that."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 5)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 100)  # huge so it'd blow through

    for i in range(20):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_write", "arguments": "{}"},
            }],
        })

    messages: list[dict] = []
    output, trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_WriteTool()), approver=_approve,
        stream=False,
        mode="plan",  # plan mode so user extension never fires
    )
    extensions = [t for t in trace if t.get("type") == "progress_extension"]
    # Every progress_extension entry must have new_cap <= hard_cap*2 == 10
    for e in extensions:
        assert e["new_cap"] <= 10, f"progress_extension exceeded 2x: {e}"


def test_step_limit_message_includes_actual_step_count(monkeypatch, queued_chat):
    """The user-facing failure message should show the ACTUAL step
    count where we stopped, not a literal config constant."""
    monkeypatch.setattr(config, "STEP_SOFT_CAP", 1000)
    monkeypatch.setattr(config, "STEP_HARD_CAP", 3)
    monkeypatch.setattr(config, "STEP_PROGRESS_GRACE", 0)

    for i in range(5):
        queued_chat.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "tool_read", "arguments": "{}"},
            }],
        })

    messages: list[dict] = []
    output, _trace = executor.chat(
        messages=messages, user_input="x",
        tools=_registry(_ReadTool()), approver=_approve,
        stream=False,
        mode="plan",
    )
    # Hard cap = 3, plan mode never extends, message should reflect step 3.
    assert "stopped" in output
    assert "3" in output


# ---------- backward compat: JANUS_MAX_STEPS env alias ----------


def test_max_steps_alias_equals_hard_cap():
    """config.MAX_STEPS must remain an alias for STEP_HARD_CAP so old
    code reading it still works."""
    assert config.MAX_STEPS == config.STEP_HARD_CAP


def test_legacy_janus_max_steps_env_sets_hard_cap(monkeypatch, tmp_path):
    """If a user has only JANUS_MAX_STEPS set (legacy single-knob), it
    feeds STEP_HARD_CAP. Soft cap defaults to half."""
    monkeypatch.setenv("JANUS_MAX_STEPS", "30")
    monkeypatch.delenv("JANUS_STEP_HARD_CAP", raising=False)
    monkeypatch.delenv("JANUS_STEP_SOFT_CAP", raising=False)
    # Re-import config to pick up env. Reload via importlib.
    import importlib
    import janus.config as cfg
    importlib.reload(cfg)
    try:
        assert cfg.STEP_HARD_CAP == 30
        assert cfg.STEP_SOFT_CAP == 15  # 30 // 2
        assert cfg.MAX_STEPS == 30
    finally:
        # Restore original config so other tests don't see the reload.
        monkeypatch.delenv("JANUS_MAX_STEPS", raising=False)
        importlib.reload(cfg)
