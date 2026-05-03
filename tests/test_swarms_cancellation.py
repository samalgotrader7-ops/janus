"""Tests for v1.4 cooperative cancellation.

Two tiers:
  1. In-process threading.Event — runner passes into each sub-agent's
     executor; checked between steps.
  2. Cross-process cancel.flag file — written by `janus swarm cancel`;
     a watcher thread mirrors it into the in-process event.

Cancellation is COOPERATIVE: sub-agents finish their current step then
exit. We don't kill threads — Python doesn't support clean forced cancel.
"""
from __future__ import annotations
import threading
import time

import pytest

from janus import config, executor, subagent
from janus.swarms import cancel, runner, spec, state
from janus.tools import default_registry
from janus.tools.capabilities import CapabilitySet


# ---------- CancellationToken primitive ----------


def test_token_initially_not_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    t = cancel.CancellationToken("r1")
    assert not t.is_cancelled()
    assert not t.event.is_set()


def test_token_cancel_sets_event_and_writes_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "r1"
    state.init_run_dir(rid)
    t = cancel.CancellationToken(rid)
    t.cancel()
    assert t.is_cancelled()
    assert t.event.is_set()
    assert state.is_cancelled(rid)


def test_token_detects_external_flag_file(tmp_path, monkeypatch):
    """If something else writes the flag, is_cancelled() picks it up
    AND mirrors into the in-process event."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "r1"
    state.init_run_dir(rid)
    t = cancel.CancellationToken(rid)
    state.write_cancel_flag(rid)  # Simulate external `janus swarm cancel`
    assert t.is_cancelled()
    assert t.event.is_set()  # mirror happened


def test_watcher_picks_up_flag_set_after_start(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    rid = "r1"
    state.init_run_dir(rid)
    t = cancel.CancellationToken(rid, poll_interval_s=0.05)
    t.start_watcher()
    try:
        time.sleep(0.1)  # Let watcher start
        assert not t.event.is_set()
        state.write_cancel_flag(rid)
        # Watcher should set event within ~poll_interval
        for _ in range(20):
            if t.event.is_set():
                break
            time.sleep(0.05)
        assert t.event.is_set()
    finally:
        t.stop_watcher()


def test_stop_watcher_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    t = cancel.CancellationToken("r1", poll_interval_s=0.05)
    t.start_watcher()
    t.stop_watcher()
    t.stop_watcher()  # Should not raise


def test_start_watcher_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    t = cancel.CancellationToken("r1", poll_interval_s=0.05)
    t.start_watcher()
    first = t._watcher
    t.start_watcher()  # Should not start a second watcher
    assert t._watcher is first
    t.stop_watcher()


# ---------- executor.execute cancellation ----------


def test_executor_execute_returns_cancelled_when_event_set(monkeypatch):
    """execute() should detect cancel_event between steps."""
    # Stub llm.chat to return a tool-call response that would loop forever.
    call_count = {"n": 0}

    def fake_chat(messages, **kw):
        call_count["n"] += 1
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"call_{call_count['n']}", "type": "function",
                "function": {"name": "fake_tool", "arguments": "{}"},
            }],
        }
    from janus import llm
    monkeypatch.setattr(llm, "chat", fake_chat)

    # Set event BEFORE execute starts → returns cancelled on first check.
    event = threading.Event()
    event.set()

    tools = default_registry(capabilities=CapabilitySet(), tool_names=[])
    output, trace = executor.execute(
        original_request="test",
        chosen_label="t",
        chosen_action="a",
        tools=tools,
        approver=lambda *a, **kw: True,
        on_step=None,
        cancel_event=event,
    )
    assert output == "[cancelled]"
    assert any(r["type"] == "cancelled" for r in trace)
    # Should never have called LLM (cancellation checked at top of step 0).
    assert call_count["n"] == 0


def test_executor_execute_no_cancel_event_runs_normally(monkeypatch):
    """When cancel_event is None, behavior is unchanged."""
    from janus import llm

    def fake_chat(messages, **kw):
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(llm, "chat", fake_chat)

    tools = default_registry(capabilities=CapabilitySet(), tool_names=[])
    output, trace = executor.execute(
        original_request="test", chosen_label="t", chosen_action="a",
        tools=tools, approver=lambda *a, **kw: True,
    )
    assert output == "done"


def test_executor_chat_returns_cancelled(monkeypatch):
    from janus import llm

    def fake_chat(messages, **kw):
        return {"role": "assistant", "content": "ok"}

    monkeypatch.setattr(llm, "chat", fake_chat)

    event = threading.Event()
    event.set()

    tools = default_registry(capabilities=CapabilitySet(), tool_names=[])
    output, trace = executor.chat(
        messages=[],
        user_input="hi",
        tools=tools,
        approver=lambda *a, **kw: True,
        stream=False,
        cancel_event=event,
    )
    assert output == "[cancelled]"


# ---------- subagent._run_in_process forwards cancel_event ----------


def test_subagent_run_in_process_passes_cancel_event(monkeypatch):
    captured = {"kw": None}

    def fake_execute(**kw):
        captured["kw"] = kw
        return "ok", []

    monkeypatch.setattr(executor, "execute", fake_execute)

    event = threading.Event()
    spec_obj = subagent.SubagentSpec(
        leaf_id="x", parent_id="p", description="d",
        request="r", label="l", action="a",
    )
    subagent._run_in_process(spec_obj, cancel_event=event)
    assert captured["kw"]["cancel_event"] is event


def test_subagent_run_in_process_omits_kwarg_when_none(monkeypatch):
    """Back-compat: stubs of executor.execute that don't accept
    cancel_event still work when the runner doesn't pass one."""
    captured = {"kw": None}

    def fake_execute(**kw):
        captured["kw"] = kw
        return "ok", []

    monkeypatch.setattr(executor, "execute", fake_execute)

    spec_obj = subagent.SubagentSpec(
        leaf_id="x", parent_id="p", description="d",
        request="r", label="l", action="a",
    )
    subagent._run_in_process(spec_obj)  # no cancel_event
    assert "cancel_event" not in captured["kw"]


# ---------- Runner end-to-end ----------


@pytest.fixture
def slow_subagent(monkeypatch):
    """Stub _run_in_process that respects cancel_event and simulates work."""
    state_box: dict = {"calls": 0, "cancelled_count": 0}

    def _stub(spec_obj, *, cancel_event=None):
        state_box["calls"] += 1
        # Simulate a few "steps" of work, checking cancel between each.
        for _ in range(5):
            if cancel_event is not None and cancel_event.is_set():
                state_box["cancelled_count"] += 1
                return subagent.SubagentResult(
                    leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
                    output="[cancelled]",
                    trace=[{"step": 0, "type": "cancelled"}],
                    error=None,
                )
            time.sleep(0.01)
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output=f"ok:{spec_obj.leaf_id}",
            trace=[{"step": 0, "type": "final", "text": "ok"}],
            error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _stub)
    return state_box


def test_runner_kills_swarm_when_flag_set_pre_phase(
    tmp_path, monkeypatch, slow_subagent,
):
    """Cancel before phase 0 starts → swarm exits with cancelled error."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: precancel
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    # Pre-create the run dir + flag (bit unusual — we cancel BEFORE the
    # swarm starts. Real flow: another process writes the flag while the
    # swarm runs.)
    rid = state.new_run_id()
    state.init_run_dir(rid)
    state.write_cancel_flag(rid)
    # We can't trivially inject our pre-made rid, but we can monkeypatch
    # new_run_id to return ours.
    monkeypatch.setattr(state, "new_run_id", lambda: rid)

    result = runner.run_swarm(s, inputs={})
    assert result.error == "cancelled"
    assert slow_subagent["calls"] == 0  # never dispatched


def test_runner_in_flight_subagent_sees_cancellation(
    tmp_path, monkeypatch, slow_subagent,
):
    """Cancel while phase 0 is running → currently-running sub-agents
    exit between steps."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    # Make the sub-agent slow enough that we can cancel mid-flight.
    def _slow_stub(spec_obj, *, cancel_event=None):
        slow_subagent["calls"] += 1
        # Wait for cancel up to 2 seconds.
        for _ in range(40):
            if cancel_event is not None and cancel_event.is_set():
                slow_subagent["cancelled_count"] += 1
                return subagent.SubagentResult(
                    leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
                    output="[cancelled]",
                    trace=[{"step": 0, "type": "cancelled"}],
                    error=None,
                )
            time.sleep(0.05)
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="ok", trace=[], error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _slow_stub)

    s = spec.parse_spec("""---
name: midcancel
type: swarm
phases:
  go:
    pattern: single
    role: w
    aggregator: concat
---
body
""")

    # Pre-stage rid so we can write the flag from outside.
    rid = state.new_run_id()
    monkeypatch.setattr(state, "new_run_id", lambda: rid)

    # Schedule cancellation 0.2s after start.
    def _cancel_after_delay():
        time.sleep(0.2)
        state.init_run_dir(rid)
        state.write_cancel_flag(rid)

    canceller = threading.Thread(target=_cancel_after_delay, daemon=True)
    canceller.start()

    result = runner.run_swarm(s, inputs={})
    canceller.join(timeout=5)

    # Sub-agent was dispatched and saw the cancel mid-loop.
    assert slow_subagent["calls"] == 1
    assert slow_subagent["cancelled_count"] == 1
    # Sub-agent's output string is "[cancelled]"; concat of one item = same string.
    assert result.phases[0].sub_agents[0].output == "[cancelled]"


def test_runner_writes_cancelled_final_json(
    tmp_path, monkeypatch, slow_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: writefinal
type: swarm
phases:
  a:
    pattern: single
    role: w
    aggregator: concat
  b:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    rid = state.new_run_id()
    state.init_run_dir(rid)
    state.write_cancel_flag(rid)
    monkeypatch.setattr(state, "new_run_id", lambda: rid)

    runner.run_swarm(s, inputs={})
    final = state.read_final(rid)
    assert final["error"] == "cancelled"


def test_runner_records_cancellation_in_timeline(
    tmp_path, monkeypatch, slow_subagent,
):
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)
    s = spec.parse_spec("""---
name: tl
type: swarm
phases:
  a:
    pattern: single
    role: w
    aggregator: concat
---
body
""")
    rid = state.new_run_id()
    state.init_run_dir(rid)
    state.write_cancel_flag(rid)
    monkeypatch.setattr(state, "new_run_id", lambda: rid)

    runner.run_swarm(s, inputs={})
    timeline = state.read_timeline(rid)
    types = [e["type"] for e in timeline]
    assert "swarm_cancelled" in types


def test_runner_stops_dispatching_subsequent_phases_on_cancel(
    tmp_path, monkeypatch, slow_subagent,
):
    """A swarm with 2 phases: cancel after phase A starts → phase B
    never dispatches."""
    monkeypatch.setattr(config, "SWARM_RUNS_DIR", tmp_path)

    s = spec.parse_spec("""---
name: stopnext
type: swarm
phases:
  a:
    pattern: single
    role: w
    aggregator: concat
  b:
    pattern: single
    role: w
    aggregator: concat
---
body
""")

    rid = state.new_run_id()
    state.init_run_dir(rid)
    monkeypatch.setattr(state, "new_run_id", lambda: rid)

    # Stub: phase A runs normally; the moment phase B's sub-agent is
    # dispatched, the test fails. We simulate cancellation between phases.
    call_count = {"n": 0}

    def _stub(spec_obj, *, cancel_event=None):
        call_count["n"] += 1
        # After A completes, write the cancel flag.
        if "a" in spec_obj.label:
            result = subagent.SubagentResult(
                leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
                output="A done", trace=[], error=None,
            )
            # Trigger cancellation — phase B should not dispatch.
            state.write_cancel_flag(rid)
            return result
        return subagent.SubagentResult(
            leaf_id=spec_obj.leaf_id, parent_id=spec_obj.parent_id,
            output="B should not run", trace=[], error=None,
        )

    monkeypatch.setattr(subagent, "_run_in_process", _stub)

    result = runner.run_swarm(s, inputs={})
    assert result.error == "cancelled"
    # Only phase A ran (1 dispatch); phase B was skipped.
    assert call_count["n"] == 1
    assert len(result.phases) == 1
