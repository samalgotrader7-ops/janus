"""Tests for janus/app.py — the surface-agnostic event-stream core
(v1.25.0 Phase 0).

These tests pin:
  * The canonical event-type vocabulary (so a regression like adding
    `tool_started` next to `tool_call` is loud).
  * That ``app.chat_events`` yields the same events ``executor.chat``'s
    on_step would emit, in the same order, plus turn_start / turn_end
    bookends.
  * That ``app.run_turn`` is byte-identical to ``executor.chat`` for
    surfaces that need the legacy (output, trace) tuple shape.
  * That exceptions inside the worker thread surface as turn_error
    events AND re-raise to the consumer (no swallowing).

Tests use a stub executor.chat so we don't pay for real LLM calls.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from janus import app


# ---------- Vocabulary ----------


def test_event_types_is_a_frozenset_of_strings():
    assert isinstance(app.EVENT_TYPES, frozenset)
    assert all(isinstance(t, str) for t in app.EVENT_TYPES)


def test_event_types_includes_core_lifecycle_events():
    """If anyone removes one of these, surfaces break silently. Pin it."""
    required = {
        "model_start", "stream_chunk", "tool_call", "tool_result",
        "final", "turn_start", "turn_end", "turn_error",
    }
    missing = required - app.EVENT_TYPES
    assert not missing, f"event vocabulary lost types: {missing}"


def test_event_types_includes_step_budget_events():
    """v1.20 step-budget events are part of the contract too."""
    for t in ("soft_cap_warning", "progress_extension",
              "budget_extended", "step_limit_reached"):
        assert t in app.EVENT_TYPES, f"{t} missing from EVENT_TYPES"


def test_event_types_includes_recovery_and_nudge():
    for t in ("recovered_tool_call", "nudge", "cancelled"):
        assert t in app.EVENT_TYPES


def test_event_types_includes_phase0_additions():
    """v1.25.0 Phase 0 added three surface-level event types pinned
    while we were defining the vocabulary. Don't drop them."""
    for t in ("hook_fired", "memory_recall", "keepalive"):
        assert t in app.EVENT_TYPES, f"Phase 0 added {t}; lost from vocab"


# ---------- Helpers: stubbing executor.chat ----------


def _make_stub_chat(events, output="hello", raise_exc=None):
    """Return a function that mimics executor.chat: pushes each event
    through on_step, returns (output, []) — or raises raise_exc."""
    def _stub(*, on_step=None, **kw):
        if raise_exc is not None:
            raise raise_exc
        if on_step is not None:
            for e in events:
                on_step(e)
        return output, []
    return _stub


# ---------- chat_events ----------


def test_chat_events_yields_turn_start_first():
    fake_events = [
        {"type": "model_start", "step": 1},
        {"type": "final", "text": "ok"},
    ]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="ok")
        gen = app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        )
        first = next(gen)
        assert first["type"] == "turn_start"
        assert first["user_input"] == "hi"
        list(gen)  # drain


def test_chat_events_yields_turn_end_last():
    fake_events = [
        {"type": "final", "text": "ok"},
    ]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events)
        out = list(app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        ))
    assert out[-1]["type"] == "turn_end"


def test_chat_events_passes_through_executor_events_in_order():
    fake_events = [
        {"type": "model_start", "step": 1},
        {"type": "stream_chunk", "text": "hel"},
        {"type": "stream_chunk", "text": "lo"},
        {"type": "tool_call", "name": "fs_read",
         "args": {"path": "x"}, "step": 1},
        {"type": "tool_result", "name": "fs_read",
         "result_preview": "contents", "step": 1},
        {"type": "final", "text": "hello"},
    ]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="hello")
        events = list(app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        ))
    # Strip the bookends, expect exactly the stub list.
    middle = [e for e in events
              if e["type"] not in ("turn_start", "turn_end")]
    assert middle == fake_events


def test_chat_events_handles_empty_executor_emit():
    """If chat() emits zero events (e.g. NO_TOOLS path with no streaming),
    we still get turn_start + turn_end bookends and no crash."""
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat([])
        events = list(app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        ))
    types = [e["type"] for e in events]
    assert types == ["turn_start", "turn_end"]


def test_chat_events_synthesizes_unknown_type_for_typeless_dicts():
    """Defensive: if some emit site forgets to set 'type', we don't
    drop the event, we tag it 'unknown'."""
    fake_events = [
        {"step": 99, "note": "no type key here"},
        {"type": "final", "text": "x"},
    ]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="x")
        events = list(app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        ))
    assert any(e.get("type") == "unknown" for e in events)


def test_chat_events_re_raises_worker_exceptions():
    """If executor.chat raises, the consumer sees a turn_error event
    AND the exception is re-raised so callers can fall back."""
    boom = RuntimeError("provider exploded")
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat([], raise_exc=boom)
        gen = app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        )
        first = next(gen)
        assert first["type"] == "turn_start"
        err_event = next(gen)
        assert err_event["type"] == "turn_error"
        assert err_event["error"] is boom
        with pytest.raises(RuntimeError, match="provider exploded"):
            list(gen)


# ---------- run_turn ----------


def test_run_turn_returns_legacy_output_and_trace():
    fake_events = [
        {"type": "model_start", "step": 1},
        {"type": "tool_call", "name": "fs_read", "args": {}, "step": 1},
        {"type": "tool_result", "name": "fs_read",
         "result_preview": "ok", "step": 1},
        {"type": "final", "text": "answer"},
    ]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="answer")
        output, trace = app.run_turn(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        )
    assert output == "answer"
    # trace excludes turn_start / turn_end bookends — those are an
    # event-stream concern, not a legacy-trace concern.
    assert all(e["type"] not in ("turn_start", "turn_end") for e in trace)
    assert [e["type"] for e in trace] == [
        "model_start", "tool_call", "tool_result", "final",
    ]


def test_run_turn_invokes_on_step_for_each_event():
    fake_events = [
        {"type": "model_start", "step": 1},
        {"type": "final", "text": "x"},
    ]
    seen: list[str] = []
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="x")
        app.run_turn(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
            on_step=lambda e: seen.append(e["type"]),
        )
    assert seen == ["model_start", "final"]


def test_run_turn_swallows_on_step_exceptions():
    """Mirror executor.chat's contract: a buggy renderer must not break
    the chat loop."""
    fake_events = [{"type": "final", "text": "x"}]
    def _bad_renderer(_e):
        raise ValueError("renderer bug")
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="x")
        out, _ = app.run_turn(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
            on_step=_bad_renderer,
        )
    assert out == "x"


def test_run_turn_extracts_final_text_from_event_not_executor_return():
    """The output we return is whatever the 'final' event carries.
    Pinned because it's tempting to read executor.chat's return tuple
    directly — but that tuple is hidden inside the worker thread and
    we can't see it. The 'final' event IS the source of truth."""
    fake_events = [
        # executor's return value says "wrong"; the final event says "right".
        {"type": "final", "text": "right"},
    ]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="wrong")
        out, _ = app.run_turn(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        )
    assert out == "right"


# ---------- thread-safety smoke ----------


def test_chat_events_iterates_in_arrival_order_under_streaming():
    """Stub a slow event source and assert ordering is preserved."""
    import time
    fake_events = [
        {"type": "stream_chunk", "text": str(i)} for i in range(20)
    ] + [{"type": "final", "text": "done"}]

    def _slow_chat(*, on_step=None, **kw):
        for e in fake_events:
            time.sleep(0.001)
            if on_step:
                on_step(e)
        return "done", []

    with patch("janus.app.executor") as ex:
        ex.chat = _slow_chat
        events = list(app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        ))
    middle = [e for e in events
              if e["type"] not in ("turn_start", "turn_end")]
    chunks = [e for e in middle if e["type"] == "stream_chunk"]
    assert [c["text"] for c in chunks] == [str(i) for i in range(20)]
    assert middle[-1]["type"] == "final"


# ---------- backward compat surface ----------


def test_run_turn_signature_matches_executor_chat():
    """Drop-in compat: every kwarg on executor.chat must exist on
    run_turn. If someone adds a new kwarg to executor.chat, this fails
    until run_turn forwards it."""
    import inspect
    from janus import executor
    chat_kwargs = set(inspect.signature(executor.chat).parameters) - {"self"}
    run_turn_kwargs = set(inspect.signature(app.run_turn).parameters) - {"self"}
    missing = chat_kwargs - run_turn_kwargs
    assert not missing, (
        f"app.run_turn missing kwargs that executor.chat accepts: {missing}"
    )


# ---------- Consumer abandonment / cancellation ----------


def test_chat_events_cancels_on_close():
    """If the consumer calls .close() on the generator (or drops it),
    chat_events sets the cancel_event and drains the queue so the
    worker thread can exit cleanly. We assert the cancel was set."""
    import threading
    cancel_set = threading.Event()

    def _slow_chat(*, on_step=None, cancel_event=None, **kw):
        # Simulate an executor that emits one event then waits for cancel.
        if on_step:
            on_step({"type": "model_start", "step": 1})
        # Wait up to 1s for cancel; if cancel arrives, record it and exit.
        if cancel_event is not None and cancel_event.wait(timeout=1.0):
            cancel_set.set()
        return "", []

    with patch("janus.app.executor") as ex:
        ex.chat = _slow_chat
        gen = app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
        )
        # Drain turn_start + the model_start, then bail.
        next(gen)  # turn_start
        next(gen)  # model_start
        gen.close()  # consumer abandons

    # Worker should have observed cancel within the wait timeout.
    assert cancel_set.wait(timeout=2.0), (
        "worker thread did not see cancel_event after generator close"
    )


def test_chat_events_uses_caller_supplied_cancel_event_unmodified():
    """If the caller passes their own cancel_event, chat_events does
    NOT set it on normal completion — that's the caller's signal to
    own. We only manipulate cancel_event when we own it."""
    import threading
    user_cancel = threading.Event()
    fake_events = [{"type": "final", "text": "done"}]
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat(fake_events, output="done")
        list(app.chat_events(
            messages=[], user_input="hi",
            tools=None, approver=lambda *a, **k: True,
            cancel_event=user_cancel,
        ))
    assert not user_cancel.is_set(), (
        "caller's cancel_event was mutated on normal completion"
    )


# ---------- run_turn exception propagation (plan-agent ask) ----------


def test_run_turn_propagates_worker_exceptions():
    """Mirror chat_events: if executor.chat raises, run_turn re-raises
    rather than silently returning ('', []). Surfaces need this so
    they can fall back / show an error."""
    boom = RuntimeError("provider exploded")
    with patch("janus.app.executor") as ex:
        ex.chat = _make_stub_chat([], raise_exc=boom)
        with pytest.raises(RuntimeError, match="provider exploded"):
            app.run_turn(
                messages=[], user_input="hi",
                tools=None, approver=lambda *a, **k: True,
            )


# ---------- Integration: real executor.chat with stubbed LLM ----------


def test_chat_events_against_real_executor_chat_no_tools():
    """Integration smoke: app.chat_events drives the REAL executor.chat
    (no mock), with the LLM call stubbed at the boundary. Catches drift
    between executor's emit sites and the EVENT_TYPES vocabulary.

    Budget: ~2s max — runs once, no parametrize. Skipped if the real
    chat() flow happens to need an LLM provider configured at import
    time (it shouldn't, but defensive)."""
    pytest.importorskip("janus.executor")
    from janus import config
    from janus.tools.base import Registry

    # Build a stub LLM response that returns one final text + no tool_calls
    # so the chat loop exits after one step. We patch janus.llm.chat_stream
    # — that's the boundary executor.chat hits for streaming completions.
    from unittest.mock import patch as _patch

    def _fake_stream(*args, **kwargs):
        # Yield one final-text chunk shaped like the real provider streams.
        yield {"content": "stub response", "tool_calls": []}

    empty_registry = Registry([])  # no tools so executor doesn't enter tool path

    with _patch("janus.llm.chat_stream", _fake_stream), \
         _patch("janus.llm.chat", return_value={"content": "stub response",
                                                  "tool_calls": []}):
        events = list(app.chat_events(
            messages=[],
            user_input="hi",
            tools=empty_registry,
            approver=lambda *a, **k: True,
            mode="default",
            workspace=str(config.WORKSPACE),
            stream=False,  # avoid streaming path; simpler integration check
        ))

    types = [e["type"] for e in events]
    # Bookends present.
    assert types[0] == "turn_start"
    assert types[-1] == "turn_end"
    # Final event present, with the stub text.
    finals = [e for e in events if e["type"] == "final"]
    assert finals, f"no final event in {types}"
    assert finals[0].get("text") == "stub response"
    # Every event type produced is in EVENT_TYPES (catches drift).
    unknown = [t for t in types if t not in app.EVENT_TYPES]
    assert not unknown, (
        f"executor.chat emitted event types not in app.EVENT_TYPES: {unknown}"
    )
