"""
janus/app.py — surface-agnostic event-stream core (v1.25.0 Phase 0).

Pre-1.25 every surface (cli_rich, cli, headless, gateways/*) called
``executor.chat()`` directly with an ``on_step`` callback. Surfaces
re-implemented event handling, formatting, and rendering on their
own. Result: cli_rich got every new feature first, cli + tui +
gateways drifted, and "parity" became a constant patch hunt.

This module is the substrate that closes that gap. Surfaces consume
ONE thing — an iterable of events — and render however they like.
The vocabulary is fixed. Add a new event type once, every surface
sees it.

USAGE — pull-style (preferred for new surfaces):

    from janus import app

    for event in app.chat_events(messages=msgs, user_input=text,
                                 tools=registry, approver=fn,
                                 mode="default"):
        if event["type"] == "stream_chunk":
            sys.stdout.write(event["text"])
        elif event["type"] == "tool_call":
            ...
        elif event["type"] == "final":
            output = event["text"]

USAGE — convenience wrapper (drop-in for existing executor.chat callers):

    output, trace = app.run_turn(messages=msgs, user_input=text,
                                 tools=registry, approver=fn,
                                 on_step=renderer)

DESIGN — DICTS NOT DATACLASSES:
Events are dicts whose ``type`` key picks the schema. This matches the
existing trace format (executor.chat returns a list[dict] today), so
no migration cost for callers that already accumulated trace lists.
A dataclass would be cleaner in isolation but the surfaces ALREADY
deal in dicts — flipping to objects would force everyone to migrate
twice (events first, then maybe ditch dataclasses later).

DESIGN — THREAD + QUEUE FOR PHASE 0:
``executor.chat()`` is a 1000-line synchronous loop with deeply nested
state (hooks, guardrails, tool dispatch, nudge logic, step budget,
approver callbacks). Refactoring it into a Python generator in a
single release would risk subtle behavior changes. Instead Phase 0
wraps the existing function: a daemon thread runs ``chat()``, an
``on_step`` shim pushes events into a Queue, the generator yields
from the Queue. When ``chat()`` returns, we synthesize a final-marker
event and stop iteration.

The approver stays a callback kwarg — it blocks the worker thread
until the user answers. Phase 1+ may move it to a proper request /
response event pair so headless surfaces (web, telegram) don't need
their own approver bridge, but Phase 0 keeps the contract stable.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable, Iterable, Iterator

from . import executor


# ---------- Thread-local for sub-tool event forwarding (v1.27.0) ----------
#
# When a chat turn runs in a worker thread (chat_events / run_turn),
# this thread-local exposes the queue-push callback to tools that want
# to forward events to the parent's progress stream. The Subagent tool
# (tools/subagent.py) reads ``parent_on_step`` and forwards each
# subagent event wrapped with ``type='subagent_step'`` so the user
# sees streaming progress instead of a silent block while the subagent
# runs.
#
# Tools that bypass app.run_turn (direct executor.chat callers, e.g.
# legacy tests) get graceful no-op behavior — the attribute is unset,
# the read returns None, the tool just doesn't forward.

_app_thread_local = threading.local()


# ---------- Canonical event vocabulary ----------
#
# Pinned in tests/test_event_stream_vocabulary.py so a regression
# (e.g. someone adding `tool_started` next to `tool_call`) is loud.
# Add new types here AND to executor.chat's emit sites in the same PR.

EVENT_TYPES: frozenset[str] = frozenset({
    # Per-step lifecycle
    "model_start",          # before each LLM call
    "model_end",            # after each LLM call
    "stream_chunk",         # token-by-token streaming text
    "tool_call",            # model wants to invoke a tool
    "tool_result",          # tool returned (success or error)
    "recovered_tool_call",  # v1.17.2 — JSON-in-content recovery
    "nudge",                # v1.17.0 — empty/stall retry nudge
    "cancelled",            # cancel_event tripped
    "final",                # final assistant text — terminal event

    # Step-budget signals (v1.20)
    "soft_cap_warning",
    "progress_extension",
    "budget_extended",
    "step_limit_reached",

    # v1.25.0 Phase 0 surface-level events (added with the vocabulary
    # pin so we don't have to retrofit later):
    "hook_fired",           # PreToolUse / PostToolUse fired (incl. denials)
    "memory_recall",        # per-turn memory recall block (cards + legacy)
    "keepalive",            # synthesized in app.py — SSE proxy heartbeat

    # Phase 0 framework events (synthesized by app.py, not executor)
    "turn_start",           # yielded once before chat() runs
    "turn_end",             # yielded once after chat() returns
    "turn_error",           # raised exception captured as event

    # v1.27.0 — first-class Subagent tool. The parent's chat stream
    # gets a structured wrapper around each subagent event so renderers
    # can group / indent / collapse them as one logical sub-turn.
    "subagent_start",       # subagent invocation begins
    "subagent_step",        # each event from inside the subagent's chat
    "subagent_end",         # subagent invocation finished

    # v1.27.1 — verification by default. After a code edit, executor
    # runs the targeted pytest and emits this event. Renderers can
    # show a green check / red x next to the tool result.
    "verification_result",

    # v1.28.2 — budget gauge. Fires after each turn when session
    # spend just crossed a 50/80/100% threshold of JANUS_BUDGET_USD.
    # Renderers can show a banner / pause / send a notification.
    "budget_alert",

    # v1.28.3 — multi-model fall-through. Fires when the primary model
    # fails with an infra-shaped error (5xx / connection / timeout)
    # and Janus transparently retries on the next model in the
    # JANUS_MODEL_FALLBACK chain.
    "model_fallback",
})


# Sentinel object used to signal "iterator exhausted" through the queue.
# A bare None is a valid event payload to no-op on, so we use a unique
# object and never expose it.
_END = object()


def chat_events(
    *,
    messages: list[dict],
    user_input: str,
    tools: Any,
    approver: Callable[..., bool],
    skill_body: str = "",
    memory_preamble: str = "",
    mode: str = "default",
    workspace: str | None = None,
    tool_count: int | None = None,
    skill_count: int | None = None,
    temperature: float = 0.7,
    stream: bool = True,
    model: str | None = None,
    cancel_event: Any | None = None,
) -> Iterator[dict]:
    """Yield events for one chat turn.

    All kwargs match ``executor.chat`` exactly so callers migrate by
    swapping the call site, nothing else. The generator yields a
    ``turn_start`` event first, then the events emitted by chat()'s
    on_step machinery in order, then either a ``final`` (already
    emitted by chat) followed by ``turn_end``, or — on exception — a
    ``turn_error`` event followed by ``turn_end``.

    The underlying ``messages`` list is mutated in place exactly like
    ``executor.chat`` does today (the worker thread holds the same
    list reference). Callers that need the post-turn ``messages`` for
    persistence read it directly after the generator finishes.
    """
    q: queue.Queue = queue.Queue()

    def _push(step: dict) -> None:
        q.put(step)

    def _runner() -> None:
        # v1.27.0: expose the queue-push callback to sub-tools (Subagent)
        # so they can forward their own events to the parent's progress
        # stream. Cleared in finally so a leaked thread-local doesn't
        # cross between turns.
        _orig_parent = getattr(_app_thread_local, "parent_on_step", None)
        _app_thread_local.parent_on_step = _push
        try:
            executor.chat(
                messages=messages,
                user_input=user_input,
                tools=tools,
                approver=approver,
                on_step=_push,
                skill_body=skill_body,
                memory_preamble=memory_preamble,
                mode=mode,
                workspace=workspace,
                tool_count=tool_count,
                skill_count=skill_count,
                temperature=temperature,
                stream=stream,
                model=model,
                cancel_event=cancel_event,
            )
            q.put(_END)
        except BaseException as exc:
            # Surface the exception to the consumer as an event so
            # surfaces can choose to render it instead of crashing.
            # Use BaseException so KeyboardInterrupt isn't silently
            # swallowed — we want it through to the consumer.
            q.put({"type": "turn_error", "error": exc})
            q.put(_END)
        finally:
            # v1.27.0: restore the previous thread-local so nested
            # workers (rare but possible if a surface spawns its own
            # chat_events on the same thread) see consistent state.
            _app_thread_local.parent_on_step = _orig_parent

    # Caller can supply their own cancel_event for cooperative cancel
    # (Ctrl+C handlers, slash-command interrupts, etc.). If they don't,
    # we own one — used by the GeneratorExit path below to tell the
    # worker to stop when the consumer abandons us.
    owns_cancel = cancel_event is None
    if owns_cancel:
        cancel_event = threading.Event()

    thread = threading.Thread(target=_runner, name="janus-chat-turn", daemon=True)
    thread.start()

    yield {"type": "turn_start", "user_input": user_input, "mode": mode}

    try:
        while True:
            item = q.get()
            if item is _END:
                break
            # If executor.chat ever yields something un-typed (shouldn't
            # happen), pass it through with a synthetic type rather than
            # losing it.
            if isinstance(item, dict):
                if "type" not in item:
                    item = {**item, "type": "unknown"}
                yield item
                if item["type"] == "turn_error":
                    exc = item.get("error")
                    if isinstance(exc, BaseException):
                        yield {"type": "turn_end"}
                        raise exc
            # else: ignore — pure defensive
    finally:
        # Consumer abandoned us (GeneratorExit) or we hit an exception
        # mid-iteration. Either way, ask the worker to stop and drain
        # any pending events so the queue + thread can be GC'd. The
        # worker is daemon=True so it won't block process exit even if
        # this drain misses something — defense in depth.
        if owns_cancel:
            try:
                cancel_event.set()
            except Exception:
                pass
        # Best-effort drain: pull a few items with a short timeout so
        # we don't accidentally block here if the worker is genuinely
        # done already.
        for _ in range(8):
            try:
                pending = q.get(timeout=0.05)
            except queue.Empty:
                break
            if pending is _END:
                break

    yield {"type": "turn_end"}


def run_turn(
    *,
    messages: list[dict],
    user_input: str,
    tools: Any,
    approver: Callable[..., bool],
    on_step: Callable[[dict], None] | None = None,
    skill_body: str = "",
    memory_preamble: str = "",
    mode: str = "default",
    workspace: str | None = None,
    tool_count: int | None = None,
    skill_count: int | None = None,
    temperature: float = 0.7,
    stream: bool = True,
    model: str | None = None,
    cancel_event: Any | None = None,
) -> tuple[str, list[dict]]:
    """Drop-in replacement for ``executor.chat`` that consumes the
    event stream internally.

    Useful for tests + surfaces that don't want to deal with iteration
    but still want the events recorded (the trace list this returns is
    every event the generator yielded except turn_start/turn_end).

    Returns ``(final_text, trace)`` exactly like ``executor.chat``.
    """
    output = ""
    trace: list[dict] = []
    for event in chat_events(
        messages=messages,
        user_input=user_input,
        tools=tools,
        approver=approver,
        skill_body=skill_body,
        memory_preamble=memory_preamble,
        mode=mode,
        workspace=workspace,
        tool_count=tool_count,
        skill_count=skill_count,
        temperature=temperature,
        stream=stream,
        model=model,
        cancel_event=cancel_event,
    ):
        if event["type"] in ("turn_start", "turn_end"):
            continue
        # Match the legacy trace shape: events appended in order.
        trace.append(event)
        if on_step is not None:
            try:
                on_step(event)
            except Exception:
                # Mirror executor.chat's on_step contract — renderer
                # exceptions never break the loop.
                pass
        if event["type"] == "final":
            output = event.get("text", "") or ""
    return output, trace
